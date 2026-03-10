import time
import random
import itertools
import os
import requests
from datetime import datetime, timedelta

# ==========================================================
# ⚙️ AYARLAR
# ==========================================================
SETTINGS = {
    "RATED_MODE": False,
    "MAX_PARALLEL_GAMES": 2,
    "SAFETY_LOCK_TIME": 45,
    "STOP_FILE": "STOP.txt",
    "POOL_REFRESH_SECONDS": 900,
    "BLACKLIST_MINUTES": 60,

    # Matchmaking hedef puan aralıkları
    "TIER_ELITE":      (2700, 4000),
    "TIER_HIGH":       (2300, 2700),
    "TIER_MID":        (2000, 2300),
    "TIER_LOW":        (1500, 2000),

    # Zaman kontrolleri (saniye+saniye formatı, string olarak)
    "TC_ALL":          ["30", "60", "60+1", "120+1", "180", "180+2", "300", "300+3", "600", "600+5", "900+10", "1800"],
    "TC_MAX_10":       ["30", "60", "60+1", "120+1", "180", "180+2", "300", "300+3", "600"],

    "CHESS960_CHANCE": 0.10,

    # Turnuva ayarları
    "AUTO_TOURNAMENT":    True,
    "JOIN_UPCOMING_MINS": 15,
    "ONLY_BOT_TOURNEYS":  True,
    "TOURNAMENT_COOLDOWN": 600,     # Turnuvalar arası min bekleme (sn)
}

# ==========================================================
# YENİ PROTOKOL
# ==========================================================
# İNSANLAR:
#   - Min 1500 puan
#   - Puansız (casual)
#   - 0.5+0 → 30+0 her format
#   - Standart + Chess960
#
# BOTLAR:
#   - < 1500  → Reddet
#   - 1500-2000 → Puansız, max 10+0
#   - 2000-2300 → Puanlı, max 10+0
#   - 2300+    → Puanlı, 0.5+0 → 30+0 (klasik dahil)
# ==========================================================

def _parse_tc(tc_str):
    """'180+2' veya '300' gibi string'i (limit_sn, inc_sn) tuple'a çevirir."""
    if '+' in tc_str:
        parts = tc_str.split('+')
        return int(parts[0]), int(parts[1])
    return int(tc_str), 0


class Matchmaker:
    def __init__(self, client, config, active_games, token):
        self.client        = client
        self.config        = config.get("matchmaking", {})
        self.enabled       = self.config.get("allow_feed", True)
        self.active_games  = active_games
        self.my_id         = None
        self.bot_pool      = []
        self.blacklist     = {}
        self.opponent_tracker = {}
        self.last_pool_update = 0
        self.wait_timeout  = 120
        self.registered_tournaments = set()
        self.last_tournament_join   = 0
        self.token         = token
        self._initialize_id()

    def _initialize_id(self):
        try:
            self.my_id = self.client.account.get()['id']
            print(f"[Matchmaker] Bağlantı Başarılı. ID: {self.my_id}")
        except:
            self.my_id = "oxydan"

    def _is_stop_triggered(self):
        if os.path.exists(SETTINGS["STOP_FILE"]):
            if len(self.active_games) == 0:
                print("🏁 [Matchmaker] Sistem kapatılıyor.")
                os._exit(0)
            return True
        return False

    def _get_bot_rating(self, bot_id, clock_limit_sn):
        try:
            if clock_limit_sn < 180:   mode = 'bullet'
            elif clock_limit_sn < 480: mode = 'blitz'
            elif clock_limit_sn < 1500:mode = 'rapid'
            else:                       mode = 'classical'

            data = self.client.users.get_public_data(bot_id)
            return data.get('perfs', {}).get(mode, {}).get('rating', 0)
        except:
            return 0

    def _is_in_tournament_game(self):
        try:
            ongoing = self.client.games.get_ongoing()
            return any(g.get('tournamentId') or g.get('swissId') for g in ongoing)
        except:
            return False

    # ==========================================================
    # 🏆 TURNUVA YÖNETİMİ — Arena + Swiss
    # ==========================================================

    def _auth_headers(self):
        h = {"User-Agent": "OxydanBot/2.0"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _fetch_arena_tournaments(self):
        """Yaklaşan ve başlamış Arena turnuvalarını çeker."""
        try:
            r = requests.get(
                "https://lichess.org/api/tournament",
                headers=self._auth_headers(),
                timeout=10
            )
            if r.status_code == 200:
                data = r.json()
                return data.get('created', []) + data.get('started', [])
        except Exception as e:
            print(f"⚠️ [Arena] Liste çekilemedi: {e}")
        return []

    def _fetch_swiss_tournaments(self):
        """Lichess'teki yaklaşan Swiss turnuvalarını çeker (team bazlı)."""
        # Swiss turnuvaları team'e bağlıdır; bot-team'leri taramak için
        # bilinen bot teamlerini deniyoruz. Gerekirse config'e eklenebilir.
        bot_teams = ["lichess-bots", "computer-chess-club", "engine-bots"]
        swiss_list = []
        for team in bot_teams:
            try:
                r = requests.get(
                    f"https://lichess.org/api/team/{team}/swiss",
                    headers=self._auth_headers(),
                    params={"status": "created"},
                    timeout=10
                )
                if r.status_code == 200:
                    # NDJSON formatı
                    for line in r.text.strip().split('\n'):
                        if line:
                            import json
                            try:
                                swiss_list.append(json.loads(line))
                            except:
                                pass
            except Exception as e:
                print(f"⚠️ [Swiss] {team} çekilemedi: {e}")
        return swiss_list

    def _join_arena(self, tournament_id):
        try:
            r = requests.post(
                f"https://lichess.org/api/tournament/{tournament_id}/join",
                headers=self._auth_headers(),
                timeout=10
            )
            return r.status_code == 200
        except Exception as e:
            print(f"⚠️ [Arena] Katılım hatası: {e}")
            return False

    def _join_swiss(self, swiss_id):
        try:
            r = requests.post(
                f"https://lichess.org/api/swiss/{swiss_id}/join",
                headers=self._auth_headers(),
                timeout=10
            )
            return r.status_code == 200
        except Exception as e:
            print(f"⚠️ [Swiss] Katılım hatası: {e}")
            return False

    def _manage_tournaments(self):
        if not SETTINGS.get("AUTO_TOURNAMENT", True):
            return
        if (time.time() - self.last_tournament_join) < SETTINGS["TOURNAMENT_COOLDOWN"]:
            return

        print("[Matchmaker] Turnuvalar taranıyor (Arena + Swiss)...")
        joined = False

        # --- Arena ---
        for t in self._fetch_arena_tournaments():
            t_id = t.get('id')
            if t_id in self.registered_tournaments:
                continue
            name = t.get('fullName', '').lower()
            if SETTINGS.get("ONLY_BOT_TOURNEYS") and "bot" not in name:
                continue
            starts_at = t.get('startsAt', 0) / 1000
            if starts_at > 0 and (starts_at - time.time()) > (SETTINGS["JOIN_UPCOMING_MINS"] * 60):
                continue
            if self._join_arena(t_id):
                self.registered_tournaments.add(t_id)
                self.last_tournament_join = time.time()
                print(f"🏆 [Arena] KATILINDI: {t.get('fullName')}")
                joined = True
                break

        if joined:
            return

        # --- Swiss ---
        for s in self._fetch_swiss_tournaments():
            s_id = s.get('id')
            if s_id in self.registered_tournaments:
                continue
            name = s.get('name', '').lower()
            if SETTINGS.get("ONLY_BOT_TOURNEYS") and "bot" not in name:
                continue
            starts_at = s.get('startsAt', 0) / 1000
            if starts_at > 0 and (starts_at - time.time()) > (SETTINGS["JOIN_UPCOMING_MINS"] * 60):
                continue
            if self._join_swiss(s_id):
                self.registered_tournaments.add(s_id)
                self.last_tournament_join = time.time()
                print(f"🏆 [Swiss] KATILINDI: {s.get('name')}")
                break

    def _cleanup_history(self):
        if len(self.registered_tournaments) > 500:
            self.registered_tournaments.clear()
            print("🧹 [System] Turnuva hafızası temizlendi.")

    # ==========================================================
    # 📋 PROTOKOL — Meydan Okuma Kabulü
    # ==========================================================

    def is_challenge_acceptable(self, challenge):
        # Turnuva maçı oynanıyorsa yeni maç alma
        if self._is_in_tournament_game():
            return False, "Currently in a tournament game."

        # Varyant kontrolü
        variant = challenge.get('variant', {}).get('key', 'standard')
        if variant not in ['standard', 'chess960']:
            return False, f"Variant '{variant}' not supported."

        challenger = challenge.get('challenger')
        if not challenger:
            return False, "No challenger info."

        user_id   = challenger.get('id', '')
        rating    = challenger.get('rating') or 0
        title     = (challenger.get('title') or '').upper()
        is_bot    = title == 'BOT'
        rated     = challenge.get('rated', False)

        time_control = challenge.get('timeControl', {})
        if time_control.get('type') != 'clock':
            return False, "Only clock games allowed."

        limit_ms  = time_control.get('limit', 0)      # saniye cinsinden gelir berserk'te
        increment = time_control.get('increment', 0)
        # Toplam tahmini süre (40 hamle üzerinden)
        total_est = limit_ms + (increment * 40)

        # Rematch sınırı
        if self.opponent_tracker.get(user_id, 0) >= 3:
            return False, "Too many games with this opponent recently."

        # ----------------------------------------------------------
        # İNSAN PROTOKOLÜ
        # ----------------------------------------------------------
        if not is_bot:
            if rating < 1500:
                return False, "Human rating below 1500."
            if rated:
                return False, "Humans must play casual (unrated)."
            # 0.5+0 (30sn) → 30+0 (1800sn): her format kabul
            if limit_ms < 30 or total_est > 1800 + (0 * 40):
                return False, "Time control out of range for humans."
            # Üst limit: 30+0 = 1800sn
            if limit_ms > 1800:
                return False, "Max time control for humans is 30+0."
            return True, f"Accepted human ({rating})"

        # ----------------------------------------------------------
        # BOT PROTOKOLÜ
        # ----------------------------------------------------------
        if rating < 1500:
            return False, "Bot rating below 1500."

        if 1500 <= rating < 2000:
            # Puansız, max 10+0 (600sn)
            if rated:
                return False, "Bots 1500-2000 must play casual."
            if limit_ms > 600:
                return False, "Max 10+0 for bots rated 1500-2000."
            return True, f"Accepted casual bot ({rating})"

        if 2000 <= rating < 2300:
            # Puanlı, max 10+0 (600sn)
            if limit_ms > 600:
                return False, "Max 10+0 for bots rated 2000-2300."
            return True, f"Accepted rated bot ({rating})"

        # 2300+: puanlı, her format (0.5+0 → 30+0)
        if limit_ms < 30:
            return False, "Min time control is 0.5+0."
        if limit_ms > 1800:
            return False, "Max time control is 30+0."
        return True, f"Accepted elite bot ({rating})"

    # ==========================================================
    # 🤖 MATCHMAKING — Bot Havuzu & Meydan Okuma
    # ==========================================================

    def _refresh_bot_pool(self):
        now = time.time()
        if not self.bot_pool or (now - self.last_pool_update > SETTINGS["POOL_REFRESH_SECONDS"]):
            try:
                stream     = self.client.bots.get_online_bots()
                online     = list(itertools.islice(stream, 200))
                self.bot_pool = [
                    b.get('id') for b in online
                    if b.get('id') and b.get('id').lower() != (self.my_id or '').lower()
                ]
                random.shuffle(self.bot_pool)
                self.last_pool_update = now
                print(f"[Matchmaker] Bot havuzu güncellendi: {len(self.bot_pool)} bot")
            except:
                time.sleep(10)

    def _pick_tier(self):
        """Ağırlıklı rastgele tier seçimi."""
        r = random.random()
        if r < 0.50: return SETTINGS["TIER_ELITE"]
        if r < 0.80: return SETTINGS["TIER_HIGH"]
        if r < 0.95: return SETTINGS["TIER_MID"]
        return SETTINGS["TIER_LOW"]

    def _find_suitable_target(self):
        self._refresh_bot_pool()
        tier = self._pick_tier()
        now  = datetime.now()

        # Tier'e göre uygun TC ve format belirle
        if tier == SETTINGS["TIER_LOW"]:
            # 1500-2000: puansız, max 10+0
            tc_pool  = SETTINGS["TC_MAX_10"]
            is_rated = False
        elif tier == SETTINGS["TIER_MID"]:
            # 2000-2300: puanlı, max 10+0
            tc_pool  = SETTINGS["TC_MAX_10"]
            is_rated = SETTINGS["RATED_MODE"]
        else:
            # 2300+: puanlı, her format
            tc_pool  = SETTINGS["TC_ALL"]
            is_rated = SETTINGS["RATED_MODE"]

        tc_str = random.choice(tc_pool)
        limit_sn, inc_sn = _parse_tc(tc_str)

        random.shuffle(self.bot_pool)
        for bot_id in self.bot_pool[:40]:
            if bot_id in self.blacklist and self.blacklist[bot_id] > now:
                continue
            rating = self._get_bot_rating(bot_id, limit_sn)
            time.sleep(0.3)
            if tier[0] <= rating <= tier[1]:
                return bot_id, rating, limit_sn, inc_sn, is_rated

        return None, 0, 0, 0, False

    def start(self):
        if not self.enabled:
            return

        print("🚀 Matchmaker Aktif — Yeni Protokol v2 (Arena + Swiss)")
        last_cleanup = time.time()

        while True:
            try:
                # Periyodik hafıza temizliği (6 saatte bir)
                if time.time() - last_cleanup > 21600:
                    self._cleanup_history()
                    last_cleanup = time.time()

                # Turnuva taraması
                self._manage_tournaments()

                # Turnuva maçı veya durdurma sinyali varsa bekle
                if self._is_in_tournament_game() or self._is_stop_triggered():
                    time.sleep(60)
                    continue

                # Slot açıksa meydan okuma gönder
                if len(self.active_games) < SETTINGS["MAX_PARALLEL_GAMES"]:
                    target, rating, limit_sn, inc_sn, is_rated = self._find_suitable_target()

                    if target:
                        variant = 'chess960' if random.random() < SETTINGS["CHESS960_CHANCE"] else 'standard'
                        rated_str = "Rated" if is_rated else "Casual"

                        print(f"[Matchmaker] → {target} ({rating}) | {rated_str} | {limit_sn//60}+{inc_sn} | {variant}")

                        self.blacklist[target] = datetime.now() + timedelta(minutes=SETTINGS["BLACKLIST_MINUTES"])

                        self.client.challenges.create(
                            username=target,
                            rated=is_rated,
                            variant=variant,
                            clock_limit=limit_sn,
                            clock_increment=inc_sn
                        )
                        time.sleep(SETTINGS["SAFETY_LOCK_TIME"])
                    else:
                        time.sleep(10)
                else:
                    time.sleep(10)

            except Exception as e:
                err = str(e)
                if "429" in err:
                    print(f"⚠️ [Matchmaker] Rate limit (429), {self.wait_timeout}sn bekleniyor.")
                    time.sleep(self.wait_timeout)
                    self.wait_timeout = min(self.wait_timeout * 2, 900)
                else:
                    print(f"⚠️ [Matchmaker] Hata: {err}")
                    time.sleep(30)
