import time
import random
import itertools
import os
import requests
import json
import threading
from datetime import datetime, timedelta

# ==========================================================
# ⚙️ AYARLAR
# ==========================================================
SETTINGS = {
    "RATED_MODE":            True,
    "MAX_PARALLEL_GAMES":    2,
    "SAFETY_LOCK_TIME":      45,
    "STOP_FILE":             "STOP.txt",
    "POOL_REFRESH_SECONDS":  600,
    "BLACKLIST_MINUTES":     60,
    "FAILED_CHALLENGE_BLACKLIST_MINUTES": 10,
    "CHESS960_CHANCE":       0.10,

    # Turnuva
    "AUTO_TOURNAMENT":       True,
    "JOIN_UPCOMING_MINS":    15,
    "ONLY_BOT_TOURNEYS":     True,
    "TOURNAMENT_COOLDOWN":   600,

    # Zaman kontrolleri (saniye)
    "TC_ALL":    ["30", "60", "60+1", "120+1", "180", "180+2",
                  "300", "300+3", "600", "600+5", "900+10", "1800"],
    "TC_MAX_10": ["30", "60", "60+1", "120+1", "180", "180+2",
                  "300", "300+3", "600"],

    # Tier puan aralıkları
    "TIER_ELITE": (2700, 4000),
    "TIER_HIGH":  (2300, 2700),
    "TIER_MID":   (2000, 2300),
    "TIER_LOW":   (1500, 2000),

    # Kümülatif eşikler: Low %10 | Mid %23 | High %35 | Elite %32
    "TIER_THRESHOLDS": {
        "LOW":  0.10,
        "MID":  0.33,
        "HIGH": 0.68,
    },

    # Koruma mekanizmaları
    "LOSING_STREAK_LIMIT":    3,
    "RATING_DROP_THRESHOLD":  50,
    "PROTECTION_GAME_COUNT":  10,
    "MAX_GAMES_PER_OPPONENT": 3,
    "OPPONENT_HISTORY_SECONDS": 3600,

    # Kalıcı kara liste (küçük harf)
    "PERMANENT_BLACKLIST": {
        "waychess-bot",
    },
}

_TIER_NAME = {
    (2700, 4000): "Elite",
    (2300, 2700): "High",
    (2000, 2300): "Mid",
    (1500, 2000): "Low",
}

def _parse_tc(tc_str):
    if '+' in tc_str:
        p = tc_str.split('+')
        return int(p[0]), int(p[1])
    return int(tc_str), 0


class RatingTracker:
    def __init__(self, client=None):
        self.client = client
        self.lock = threading.Lock()  # ✅ GÜNCELLEME: Thread güvenliği için kilit eklendi
        self.baseline = {
            'bullet': 2931, 'blitz': 2889,
            'rapid':  2925, 'classical': 2773, 'chess960': 2021,
        }
        self.current          = dict(self.baseline)
        self.losing_streak    = 0
        self.protection_games = 0
        self.in_protection    = False

    def initialize_baselines(self):
        """Botun başlangıç reytinglerini API'den dinamik olarak çeker."""
        if self.client:
            try:
                data  = self.client.account.get()
                perfs = data.get('perfs', {})
                with self.lock:
                    for mode in self.baseline:
                        if mode in perfs and 'rating' in perfs[mode]:
                            self.baseline[mode] = perfs[mode]['rating']
                    self.current = dict(self.baseline)
                print(f"📊 [RatingTracker] Baseline yüklendi: {self.current}")
            except Exception as e:
                print(f"⚠️ [RatingTracker] Baseline alınamadı, varsayılanlar aktif: {e}")

    def record_result(self, result, mode, new_rating=None):
        with self.lock:  # ✅ GÜNCELLEME: Çoklu oyun bitişlerinde yarış durumları engellendi
            was_in_protection = self.in_protection

            # Puan düşüşü kontrolü
            if new_rating and mode in self.current:
                old  = self.current[mode]
                self.current[mode] = new_rating
                drop = old - new_rating
                if drop >= SETTINGS["RATING_DROP_THRESHOLD"]:
                    self._activate_protection(
                        f"{mode.capitalize()} puanı {drop} puan düştü ({old}→{new_rating})"
                    )

            # Seri kontrolü
            if result == 'loss':
                self.losing_streak += 1
                if self.losing_streak >= SETTINGS["LOSING_STREAK_LIMIT"]:
                    self._activate_protection(f"{self.losing_streak} üst üste kayıp")
            else:
                self.losing_streak = 0

            # Geri sayım sadece koruma altındayken oynanan maçlar için
            if was_in_protection:
                self.protection_games -= 1
                if self.protection_games <= 0:
                    self.in_protection = False
                    self.losing_streak = 0
                    print("✅ [Koruma] Koruma modu sona erdi, normal dağılıma dönülüyor.")

    def _activate_protection(self, reason):
        if not self.in_protection:
            print(f"🛡️ [Koruma] {reason}")
            print(f"🛡️ [Koruma] Sonraki {SETTINGS['PROTECTION_GAME_COUNT']} maç Mid tier'da oynanacak.")
        self.in_protection    = True
        self.protection_games = SETTINGS["PROTECTION_GAME_COUNT"]

    def is_in_protection(self):
        with self.lock:
            return self.in_protection


class Matchmaker:
    def __init__(self, client, config, active_games, token, active_games_lock=None):
        self.client            = client
        self.raw_config        = config
        self.config            = config.get("matchmaking", {})
        self.enabled           = self.config.get("allow_feed", True)
        self.active_games      = active_games
        self.active_games_lock = active_games_lock
        self.my_id             = None
        self.bot_pool          = []
        self.blacklist         = {}
        self.opponent_tracker  = {}
        self.last_pool_update  = 0
        self.wait_timeout      = 120
        self.registered_tournaments = set()
        self.last_tournament_join   = 0
        self.last_cleanup           = 0
        self.token             = token
        self.cleanup_lock      = threading.Lock()
        self.opponent_lock     = threading.Lock()

        self._apply_config_overrides()
        self.rating_tracker = RatingTracker(self.client)
        self.rating_tracker.initialize_baselines()
        self._initialize_id()

    def _active_game_count(self):
        if self.active_games_lock:
            with self.active_games_lock:
                return len(self.active_games)
        return len(self.active_games)

    def _apply_config_overrides(self):
        """YAML ayarlarını global SETTINGS'e enjekte eder."""
        if "rated_mode" in self.config:
            SETTINGS["RATED_MODE"] = self.config["rated_mode"]
        if "max_games" in self.config:
            SETTINGS["MAX_PARALLEL_GAMES"] = self.config["max_games"]
        if "chess960_chance" in self.config:
            SETTINGS["CHESS960_CHANCE"] = self.config["chess960_chance"]
        for key in (
            "rated_mode", "safety_lock_time", "pool_refresh_seconds",
            "blacklist_minutes", "failed_challenge_blacklist_minutes",
            "max_games_per_opponent", "opponent_history_seconds",
            "auto_tournament", "tournament_cooldown",
        ):
            if key in self.config:
                SETTINGS[key.upper()] = self.config[key]
        if "permanent_blacklist" in self.config:
            yaml_bl = {b.lower() for b in self.config["permanent_blacklist"]}
            SETTINGS["PERMANENT_BLACKLIST"].update(yaml_bl)

    def _initialize_id(self):
        try:
            self.my_id = self.client.account.get()['id']
            print(f"[Matchmaker] Bağlantı Başarılı. ID: {self.my_id}")
        except Exception as e:
            print(f"⚠️ [Matchmaker] ID alınamadı: {e}")
            self.my_id = "oxydan"

    def _is_stop_triggered(self):
        if os.path.exists(SETTINGS["STOP_FILE"]):
            if self._active_game_count() == 0:
                print("🏁 [Matchmaker] Sistem kapatılıyor.")
                os._exit(0)
            return True
        return False

    def _is_in_tournament_game(self):
        try:
            ongoing = self.client.games.get_ongoing()
            return any(g.get('tournamentId') or g.get('swissId') for g in ongoing)
        except Exception as e:
            print(f"⚠️ [Matchmaker] Turnuva kontrolü başarısız: {e}")
            return False

    # ==========================================================
    # 🏆 TURNUVA YÖNETİMİ
    # ==========================================================

    def _auth_headers(self):
        h = {"User-Agent": "OxydanBot/3.0"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _fetch_arena_tournaments(self):
        try:
            r = requests.get(
                "https://lichess.org/api/tournament",
                headers=self._auth_headers(), timeout=10
            )
            if r.status_code == 429: raise Exception("HTTP 429")
            if r.status_code == 200:
                data = r.json()
                return data.get('created', []) + data.get('started', [])
        except Exception as e:
            if "429" in str(e): raise
            print(f"⚠️ [Arena] Liste çekilemedi: {e}")
        return []

    def _fetch_swiss_tournaments(self):
        bot_teams  = ["lichess-bots", "computer-chess-club", "engine-bots"]
        swiss_list = []
        for team in bot_teams:
            try:
                r = requests.get(
                    f"https://lichess.org/api/team/{team}/swiss",
                    headers=self._auth_headers(),
                    params={"status": "created"}, timeout=10
                )
                if r.status_code == 429: raise Exception("HTTP 429")
                if r.status_code == 200:
                    for line in r.text.strip().split('\n'):
                        if line:
                            try: swiss_list.append(json.loads(line))
                            except: pass
            except Exception as e:
                if "429" in str(e): raise
                print(f"⚠️ [Swiss] {team}: {e}")
        return swiss_list

    def _join_arena(self, tid):
        try:
            r = requests.post(
                f"https://lichess.org/api/tournament/{tid}/join",
                headers=self._auth_headers(), timeout=10
            )
            if r.status_code == 429: raise Exception("HTTP 429")
            return r.status_code == 200
        except Exception as e:
            if "429" in str(e): raise
            print(f"⚠️ [Arena] Katılım hatası: {e}")
            return False

    def _join_swiss(self, sid):
        try:
            r = requests.post(
                f"https://lichess.org/api/swiss/{sid}/join",
                headers=self._auth_headers(), timeout=10
            )
            if r.status_code == 429: raise Exception("HTTP 429")
            return r.status_code == 200
        except Exception as e:
            if "429" in str(e): raise
            print(f"⚠️ [Swiss] Katılım hatası: {e}")
            return False

    def _manage_tournaments(self):
        if not SETTINGS.get("AUTO_TOURNAMENT", True):
            return
        if (time.time() - self.last_tournament_join) < SETTINGS["TOURNAMENT_COOLDOWN"]:
            return

        print("[Matchmaker] Turnuvalar taranıyor (Arena + Swiss)...")

        for t in self._fetch_arena_tournaments():
            tid  = t.get('id')
            if tid in self.registered_tournaments: continue
            name = t.get('fullName', '').lower()
            if SETTINGS.get("ONLY_BOT_TOURNEYS") and "bot" not in name: continue
            starts = t.get('startsAt', 0) / 1000
            if starts > 0 and (starts - time.time()) > SETTINGS["JOIN_UPCOMING_MINS"] * 60: continue
            if self._join_arena(tid):
                self.registered_tournaments.add(tid)
                self.last_tournament_join = time.time()
                print(f"🏆 [Arena] KATILINDI: {t.get('fullName')}")
                return

        for s in self._fetch_swiss_tournaments():
            sid  = s.get('id')
            if sid in self.registered_tournaments: continue
            name = s.get('name', '').lower()
            if SETTINGS.get("ONLY_BOT_TOURNEYS") and "bot" not in name: continue
            starts = s.get('startsAt', 0) / 1000
            if starts > 0 and (starts - time.time()) > SETTINGS["JOIN_UPCOMING_MINS"] * 60: continue
            if self._join_swiss(sid):
                self.registered_tournaments.add(sid)
                self.last_tournament_join = time.time()
                print(f"🏆 [Swiss] KATILINDI: {s.get('name')}")
                return

    def _cleanup_history(self):
        with self.cleanup_lock:
            if len(self.registered_tournaments) > 500:
                self.registered_tournaments = set(
                    list(self.registered_tournaments)[-250:]
                )
                print("🧹 [Cleanup] Turnuva hafızası budandı.")

            with self.opponent_lock:
                old_count = len(self.opponent_tracker)
                self.opponent_tracker.clear()
            print(f"🧹 [Cleanup] opponent_tracker sıfırlandı ({old_count} kayıt temizlendi).")

    # ==========================================================
    # 📋 PROTOKOL — Gelen Meydan Okuma Kabulü
    # ==========================================================

    def is_challenge_acceptable(self, challenge):
        if self._is_in_tournament_game():
            return False, "Currently in a tournament game."

        variant = challenge.get('variant', {}).get('key', 'standard')
        if variant not in ['standard', 'chess960']:
            return False, f"Variant '{variant}' not supported."

        challenger = challenge.get('challenger')
        if not challenger:
            return False, "No challenger info."

        user_id = challenger.get('id', '')
        rating  = challenger.get('rating') or 0
        title   = (challenger.get('title') or '').upper()
        is_bot  = title == 'BOT'
        rated   = challenge.get('rated', False)

        if user_id.lower() in SETTINGS["PERMANENT_BLACKLIST"]:
            return False, f"{user_id} is permanently blacklisted."

        tc = challenge.get('timeControl', {})
        if tc.get('type') != 'clock':
            return False, "Only clock games allowed."

        limit_sn = tc.get('limit', 0)

        opponent_key = user_id.lower()
        with self.opponent_lock:
            games_with_user = self.opponent_tracker.get(opponent_key, 0)

        if games_with_user >= SETTINGS["MAX_GAMES_PER_OPPONENT"]:
            return False, f"Max {SETTINGS['MAX_GAMES_PER_OPPONENT']} games reached with {user_id}."

        # İNSAN
        if not is_bot:
            if rating < 1500:
                return False, "Human rating below 1500."
            if rated:
                return False, "Humans must play casual."
            if limit_sn < 30 or limit_sn > 1800:
                return False, "Time control out of range (0.5+0 to 30+0)."
            return True, f"Accepted human ({rating})"

        # BOT
        if rating < 1500:
            return False, "Bot rating below 1500."
        if 1500 <= rating < 2000:
            if rated:
                return False, "Bots 1500-2000 must play casual."
            if limit_sn > 600:
                return False, "Max 10+0 for bots 1500-2000."
            return True, f"Accepted casual bot ({rating})"
        if 2000 <= rating < 2300:
            if limit_sn > 600:
                return False, "Max 10+0 for bots 2000-2300."
            return True, f"Accepted rated bot ({rating})"

        # 2300+
        if limit_sn < 30 or limit_sn > 1800:
            return False, "Time control out of range (0.5+0 to 30+0)."
        return True, f"Accepted elite bot ({rating})"

    # ==========================================================
    # 🎯 MATCHMAKER — Akıllı Tier Seçimi
    # ==========================================================

    def _pick_tier(self):
        if self.rating_tracker.is_in_protection():
            print(f"🛡️ [Koruma] Mid kilitli — kalan: {self.rating_tracker.protection_games} maç")
            return SETTINGS["TIER_MID"]

        r = random.random()
        t = SETTINGS["TIER_THRESHOLDS"]
        if r < t["LOW"]:  return SETTINGS["TIER_LOW"]
        if r < t["MID"]:  return SETTINGS["TIER_MID"]
        if r < t["HIGH"]: return SETTINGS["TIER_HIGH"]
        return SETTINGS["TIER_ELITE"]

    def _refresh_bot_pool(self):
        now = time.time()
        if not self.bot_pool or (now - self.last_pool_update > SETTINGS["POOL_REFRESH_SECONDS"]):
            try:
                stream = self.client.bots.get_online_bots()
                online = list(itertools.islice(stream, 200))
                self.bot_pool = [
                    b.get('id') for b in online
                    if b.get('id')
                    and b.get('id').lower() != (self.my_id or '').lower()
                    and b.get('id', '').lower() not in SETTINGS["PERMANENT_BLACKLIST"]
                ]
                random.shuffle(self.bot_pool)
                self.last_pool_update = now
                print(f"[Matchmaker] Bot havuzu: {len(self.bot_pool)} bot")
            except Exception as e:
                if "429" in str(e): raise
                print(f"⚠️ [Matchmaker] Havuz yenileme hatası: {e}")
                time.sleep(10)

    def _find_suitable_target(self):
        self._refresh_bot_pool()
        tier      = self._pick_tier()
        tier_name = _TIER_NAME.get(tier, "?")
        now       = datetime.now()

        if tier == SETTINGS["TIER_LOW"]:
            tc_pool  = SETTINGS["TC_MAX_10"]
            is_rated = False
        elif tier == SETTINGS["TIER_MID"]:
            tc_pool  = SETTINGS["TC_MAX_10"]
            is_rated = False if self.rating_tracker.is_in_protection() else SETTINGS["RATED_MODE"]
        else:
            tc_pool  = SETTINGS["TC_ALL"]
            is_rated = SETTINGS["RATED_MODE"]

        tc_str           = random.choice(tc_pool)
        limit_sn, inc_sn = _parse_tc(tc_str)

        if limit_sn < 180:    mode = 'bullet'
        elif limit_sn < 480:  mode = 'blitz'
        elif limit_sn < 1500: mode = 'rapid'
        else:                 mode = 'classical'

        # ✅ GÜNCELLEME: opponent_tracker okuması kilit altına alındı
        with self.opponent_lock:
            candidates = [
                b for b in self.bot_pool
                if (b.lower() not in self.blacklist or self.blacklist[b.lower()] <= now)
                and self.opponent_tracker.get(b.lower(), 0) < SETTINGS["MAX_GAMES_PER_OPPONENT"]
            ][:50]

        if not candidates:
            return None, 0, 0, 0, False, tier_name

        try:
            r = requests.post(
                "https://lichess.org/api/users",
                headers=self._auth_headers(),
                data=",".join(candidates),
                timeout=10
            )
            if r.status_code == 429:
                raise Exception("HTTP 429 Rate Limit")  # ✅ GÜNCELLEME: Ana döngünün yakalaması sağlandı
            if r.status_code == 200:
                users_data = r.json()
                random.shuffle(users_data)
                for user in users_data:
                    bot_id = user.get('id')
                    rating = user.get('perfs', {}).get(mode, {}).get('rating', 0)
                    if tier[0] <= rating <= tier[1]:
                        return bot_id, rating, limit_sn, inc_sn, is_rated, tier_name
            else:
                raise Exception(f"HTTP {r.status_code}")
        except Exception as e:
            if "429" in str(e):
                raise  # ✅ GÜNCELLEME: Rate limit bypass edilmiyor, üst metoda fırlatılıyor
            print(f"⚠️ [Matchmaker] Toplu çekme başarısız: {e} — tekli moda geçildi")
            for bot_id in candidates[:5]:
                try:
                    data   = self.client.users.get_public_data(bot_id)
                    rating = data.get('perfs', {}).get(mode, {}).get('rating', 0)
                    time.sleep(0.3)
                    if tier[0] <= rating <= tier[1]:
                        return bot_id, rating, limit_sn, inc_sn, is_rated, tier_name
                except Exception as ex:
                    if "429" in str(ex): raise
                    continue

        return None, 0, 0, 0, False, tier_name

    def record_game_result(self, result, mode, new_rating=None, opponent_id=None):
        self.rating_tracker.record_result(result, mode, new_rating)

        if opponent_id:
            opponent_key = opponent_id.lower()
            with self.opponent_lock:
                self.opponent_tracker[opponent_key] = (
                    self.opponent_tracker.get(opponent_key, 0) + 1
                )

    # ==========================================================
    # 🚀 ANA DÖNGÜ
    # ==========================================================

    def start(self):
        if not self.enabled:
            print("🚫 Matchmaker YAML ile devre dışı.")
            return

        print("🚀 Matchmaker v3.6 Aktif — Oxydan Aegis Protokolü")
        print("   Dağılım: Elite %32 | High %35 | Mid %23 | Low %10")
        print(f"   Max per opponent: {SETTINGS['MAX_GAMES_PER_OPPONENT']}")

        while True:
            try:
                if time.time() - self.last_cleanup > SETTINGS["OPPONENT_HISTORY_SECONDS"]:
                    self._cleanup_history()
                    self.last_cleanup = time.time()

                self._manage_tournaments()

                if self._is_in_tournament_game() or self._is_stop_triggered():
                    time.sleep(60)
                    continue

                if self._active_game_count() < SETTINGS["MAX_PARALLEL_GAMES"]:
                    target, rating, limit_sn, inc_sn, is_rated, tier_name = \
                        self._find_suitable_target()

                    if target:
                        variant   = 'chess960' if random.random() < SETTINGS["CHESS960_CHANCE"] else 'standard'
                        rated_str = "Rated" if is_rated else "Casual"
                        mins      = limit_sn // 60
                        secs      = limit_sn % 60
                        tc_label  = f"{mins}:{secs:02d}+{inc_sn}" if secs else f"{mins}+{inc_sn}"
                        with self.opponent_lock:
                            played = self.opponent_tracker.get(target.lower(), 0)

                        print(
                            f"[{tier_name}] → {target} ({rating}) | "
                            f"{rated_str} | {tc_label} | {variant} | "
                            f"Oyun {played}/{SETTINGS['MAX_GAMES_PER_OPPONENT']}"
                        )

                        target_key = target.lower()
                        self.blacklist[target_key] = datetime.now() + timedelta(
                            minutes=SETTINGS["BLACKLIST_MINUTES"]
                        )
                        try:
                            self.client.challenges.create(
                                username=target,
                                rated=is_rated,
                                variant=variant,
                                clock_limit=limit_sn,
                                clock_increment=inc_sn
                            )
                            self.wait_timeout = 120
                        except Exception as ce:
                            if "429" in str(ce): raise
                            self.blacklist[target_key] = datetime.now() + timedelta(
                                minutes=SETTINGS["FAILED_CHALLENGE_BLACKLIST_MINUTES"]
                            )
                            raise
                        time.sleep(SETTINGS["SAFETY_LOCK_TIME"])
                    else:
                        # ✅ GÜNCELLEME: Uygun bot bulunamazsa Lichess API'sini spamlamamak için süre artırıldı
                        time.sleep(45)
                else:
                    time.sleep(10)

            except Exception as e:
                err = str(e)
                if "429" in err:
                    print(f"⚠️ Rate limit (429), {self.wait_timeout}sn bekleniyor.")
                    time.sleep(self.wait_timeout)
                    self.wait_timeout = min(self.wait_timeout * 2, 900)
                else:
                    print(f"⚠️ [Matchmaker] Hata: {err}")
                    time.sleep(30)
