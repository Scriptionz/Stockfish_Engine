import time
import random
import itertools
import os
import requests
from datetime import datetime, timedelta

# ==========================================================
# ⚙️ GÜNCELLENMİŞ AYARLAR
# ==========================================================
SETTINGS = {
    "RATED_MODE": True,
    "MAX_PARALLEL_GAMES": 2,
    "SAFETY_LOCK_TIME": 45,
    "STOP_FILE": "STOP.txt",
    "POOL_REFRESH_SECONDS": 900,
    "BLACKLIST_MINUTES": 60,
    "TIER_HIGH": (2700, 4000),
    "TIER_MID": (2000, 2700),
    "TIER_LOW": (1500, 2000),
    "TIME_CONTROLS": ["0.5+0", "1+0", "1+1", "2+1", "3+0", "3+2", "5+0", "5+3", "10+0", "10+5", "15+10", "30+0"],
    "CHESS960_CHANCE": 0.10,

    # --- Turnuva Ayarları ---
    "AUTO_TOURNAMENT": True,        # Turnuvalara otomatik katılsın mı?
    "JOIN_UPCOMING_MINS": 15,       # Başlamasına X dakika kalanlara gir
    "ONLY_BOT_TOURNEYS": True       # Sadece isminde "Bot" geçen turnuvaları tercih et
}

class Matchmaker:
    def __init__(self, client, config, active_games, token): 
        self.client = client
        self.config = config.get("matchmaking", {})
        self.enabled = self.config.get("allow_feed", True)
        self.active_games = active_games  
        self.my_id = None
        self.bot_pool = []
        self.blacklist = {}
        self.opponent_tracker = {}
        self.last_pool_update = 0
        self.wait_timeout = 120
        self.registered_tournaments = set()
        self.last_tournament_join = 0
        self.tournament_cooldown = 600
        self.token = token
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
                print(f"🏁 [Matchmaker] Sistem kapatılıyor.")
                os._exit(0)
            return True
        return False

    def _get_bot_rating(self, bot_id, clock_limit): 
        try:
            # Süreye göre mod belirleme protokolü
            if clock_limit < 180: mode = 'bullet'
            elif clock_limit < 480: mode = 'blitz'
            elif clock_limit < 1500: mode = 'rapid'
            else: mode = 'classical'
            
            user_data = self.client.users.get_public_data(bot_id)
            return user_data.get('perfs', {}).get(mode, {}).get('rating', 0)
        except: 
            return 0

    def _is_in_tournament_game(self):
        """Aktif bir turnuva maçında olup olmadığını kontrol eder."""
        try:
            ongoing = self.client.games.get_ongoing()
            # ongoing liste olarak döner, herhangi bir turnuva maçı varsa True dön
            return any(game.get('tournamentId') is not None for game in ongoing)
        except: 
            return False

    # ==========================================================
    # 🏆 DOĞRUDAN API İLE TURNUVA YÖNETİMİ
    # ==========================================================
    def _fetch_created_tournaments(self):
        url = "https://lichess.org/api/tournament"
        # User-Agent eklemek Lichess'in bağlantıyı koparmasını engeller
        headers = {"User-Agent": "OxydanBot/1.0"} 
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
            
        try:
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                # 'started' ve 'created' listelerini birleştirerek tarama yapalım
                return data.get('created', []) + data.get('started', [])
            return []
        except Exception as e:
            print(f"⚠️ [API] Turnuva listesi çekilemedi (Bağlantı Hatası): {e}")
            return []

    def _join_tournament(self, tournament_id):
        """Lichess API üzerinden turnuvaya katılım isteği (POST)."""
        # Endpoint: https://lichess.org/api/tournament/{id}/join (POST)
        url = f"https://lichess.org/api/tournament/{tournament_id}/join"
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
            
        try:
            response = requests.post(url, headers=headers, timeout=10)
            return response.status_code == 200
        except Exception as e:
            print(f"⚠️ [API] Turnuvaya katılınamadı: {e}")
            return False

    def _manage_tournaments(self):
        if not SETTINGS.get("AUTO_TOURNAMENT", True): return
        
        # Mola süresini kontrol et
        if (time.time() - self.last_tournament_join) < self.tournament_cooldown:
            return

        print("[Matchmaker] Yaklaşan turnuvalar taranıyor...") # Log ekledik
        tourneys = self._fetch_created_tournaments()
        
        for t in tourneys:
            t_id = t.get('id')
            if t_id in self.registered_tournaments: continue
            
            name = t.get('fullName', '').lower()
            # Filtreyi logla takip edelim
            if SETTINGS.get("ONLY_BOT_TOURNEYS") and "bot" not in name:
                continue

            starts_at = t.get('startsAt', 0) / 1000
            # Eğer turnuva 15 dakikadan (JOIN_UPCOMING_MINS) daha uzaksa bekle
            if starts_at > 0 and (starts_at - time.time()) > (SETTINGS.get("JOIN_UPCOMING_MINS", 15) * 60):
                continue

            # Katılma isteği gönder
            if self._join_tournament(t_id):
                self.registered_tournaments.add(t_id)
                self.last_tournament_join = time.time()
                print(f"🏆 [Tournament] BAŞARIYLA KATILINDI: {t.get('fullName')}")
                break
            
    def _cleanup_history(self):
        """Çok eski turnuva kayıtlarını bellekten atar."""
        if len(self.registered_tournaments) > 500: 
            self.registered_tournaments.clear() 
            print("🧹 [System] Turnuva kayıt hafızası temizlendi.")

    def is_challenge_acceptable(self, challenge):
        """Sınıf içi yapıya uygun, tüm protokolleri koruyan kabul edici."""
        
        # Turnuva kontrolü
        if self._is_in_tournament_game():
            return False, "I am currently playing a tournament game."
        
        # Varyant Filtresi
        variant = challenge.get('variant', {}).get('key')
        if variant not in ['standard', 'chess960']:
            return False, f"Variant '{variant}' is not supported."
        
        challenger = challenge.get('challenger')
        if not challenger: 
            return False, "Generic challenge"
        
        rating = challenger.get('rating') or 1500
        title = challenger.get('title', '') or ''
        is_bot = title.upper() == 'BOT'
        
        rated = challenge.get('rated', False)
        user_id = challenger['id']
        
        time_control = challenge.get('timeControl', {})
        if time_control.get('type') != 'clock':
            return False, "Only standard clock games allowed"
        
        limit = time_control.get('limit', 0)
        increment = time_control.get('increment', 0)
        total_est_time = limit + (increment * 40)
        
        if self.opponent_tracker.get(user_id, 0) >= 3: 
            return False, "Too many games recently"
        
        # Protokoller
        if is_bot:
            if rating >= 2000:
                if total_est_time <= 1800: return True, "Accepted Masters Bot"
                return False, "Total time too long for Masters"
            elif 1500 <= rating < 2000:
                if rated: return False, "Challengers must play Casual"
                if total_est_time <= 300: return True, "Accepted Casual Challenger"
                return False, "Total time too long for Challenger"
            return False, "Bot rating too low"
        else:
            if rated: return False, "Humans must play Casual"
            if total_est_time <= 600: return True, "Accepted Casual Human"
            return False, "Human time limit exceeded"
            
    def _refresh_bot_pool(self):
        now = time.time()
        if not self.bot_pool or (now - self.last_pool_update > SETTINGS["POOL_REFRESH_SECONDS"]):
            try:
                stream = self.client.bots.get_online_bots()
                online_bots = list(itertools.islice(stream, 150))
                self.bot_pool = [b.get('id') for b in online_bots if b.get('id') and b.get('id').lower() != self.my_id.lower()]
                random.shuffle(self.bot_pool)
                self.last_pool_update = now
            except: time.sleep(10)

    def _find_suitable_target(self, clock_limit):
        self._refresh_bot_pool()
        now = datetime.now()
        roll = random.random()
        target_range = SETTINGS["TIER_HIGH"] if roll < 0.75 else (SETTINGS["TIER_MID"] if roll < 0.95 else SETTINGS["TIER_LOW"])

        random.shuffle(self.bot_pool)
        for bot_id in self.bot_pool[:30]:
            if bot_id in self.blacklist and self.blacklist[bot_id] > now: continue
            rating = self._get_bot_rating(bot_id, clock_limit)
            time.sleep(0.4) 
            if target_range[0] <= rating <= target_range[1]:
                return bot_id, rating
        return None, 0

    def start(self):
        if not self.enabled: 
            return
        
        print(f"🚀 Void 3 Hybrid Manager Aktif. (Katı Protokol: 1500-2000 Puansız/Max 5+0)")
        last_cleanup_time = time.time()

        while True:
            try:
                if time.time() - last_cleanup_time > 21600:
                    self._cleanup_history()
                    last_cleanup_time = time.time()
                    
                self._manage_tournaments()

                if self._is_in_tournament_game() or self._is_stop_triggered():
                    time.sleep(60)
                    continue

                if len(self.active_games) < SETTINGS["MAX_PARALLEL_GAMES"]:
                    target, target_rating = self._find_suitable_target(1800)
                    
                    if target:
                        if 1500 <= target_rating < 2000:
                            is_rated = False
                            tc = random.choice(["0.5+0", "1+0", "2+1", "3+0", "3+2", "5+0"])
                        else:
                            is_rated = SETTINGS["RATED_MODE"]
                            tc = random.choice(SETTINGS["TIME_CONTROLS"])

                        t_limit_raw, t_inc = map(float, tc.split('+'))
                        clock_limit_seconds = int(t_limit_raw * 60)
                        
                        variant = 'chess960' if random.random() < SETTINGS["CHESS960_CHANCE"] else 'standard'
                        
                        print(f"[Matchmaker] -> {target} ({target_rating}) | Rated: {is_rated} | TC: {tc}")
                        self.blacklist[target] = datetime.now() + timedelta(minutes=SETTINGS["BLACKLIST_MINUTES"])
                        
                        self.client.challenges.create(
                            username=target, 
                            rated=is_rated, 
                            variant=variant,
                            clock_limit=clock_limit_seconds, 
                            clock_increment=int(t_inc)
                        )
                        time.sleep(SETTINGS["SAFETY_LOCK_TIME"])
                    else:
                        time.sleep(10)
                else:
                    time.sleep(10)

            except Exception as e:
                error_str = str(e)
                if "429" in error_str:
                    print(f"⚠️ [Matchmaker] Hız sınırı (429), {self.wait_timeout}sn bekleniyor.")
                    time.sleep(self.wait_timeout)
                    self.wait_timeout = min(self.wait_timeout * 2, 900)
                else:
                    print(f"⚠️ [Matchmaker] Hata: {error_str}")
                    time.sleep(30)
