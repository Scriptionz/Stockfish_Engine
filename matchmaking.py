import time
import random
import itertools
import os
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
    def __init__(self, client, config, active_games): 
        self.client = client
        self.config = config.get("matchmaking", {})
        self.enabled = self.config.get("allow_feed", True)
        self.active_games = active_games  
        self.my_id = None
        self.bot_pool = []
        self.blacklist = {}
        self.last_pool_update = 0
        self.wait_timeout = 120
        self._initialize_id()
        self.registered_tournaments = set()
        self.last_tournament_join = 0
        self.tournament_cooldown = 3600

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

    def _get_bot_rating(self, bot_id):
        try:
            user_data = self.client.users.get_public_data(bot_id)
            perfs = user_data.get('perfs', {})
            ratings = [perfs.get(m, {}).get('rating', 0) for m in ['blitz', 'bullet', 'rapid', 'classical']]
            return max(ratings) if ratings else 0
        except: return 0

    def _is_in_tournament_game(self):
        """Aktif bir turnuva maçında olup olmadığını kontrol eder."""
        try:
            ongoing = self.client.games.get_ongoing()
            # ongoing liste olarak döner, herhangi bir turnuva maçı varsa True dön
            return any(game.get('tournamentId') is not None for game in ongoing)
        except: 
            return False

    def _manage_tournaments(self):
        """Yaklaşan turnuvaları tarar, mola süresini kontrol eder ve uygun olanlara katılır."""
        
        # 1. Genel ayar kontrolü
        if not SETTINGS.get("AUTO_TOURNAMENT", True): 
            return

        # 2. Mola (Cooldown) kontrolü
        # Eğer en son turnuvaya katılmamızdan bu yana 'tournament_cooldown' kadar süre geçmediyse işlem yapma
        if (time.time() - self.last_tournament_join) < self.tournament_cooldown:
            return

        try:
            tourneys = self.client.tournaments.get_all()
            for t in tourneys:
                # Zaten işlem yapılmışsa (veya katılmışsak) atla
                if t['id'] in self.registered_tournaments: 
                    continue
                
                # Sadece yaklaşan (created) turnuvaları kontrol et
                if t.get('status') == 'created':
                    name = t.get('fullName', '').lower()
                    
                    # Filtre: Sadece bot turnuvaları mı?
                    if SETTINGS.get("ONLY_BOT_TOURNEYS", True) and "bot" not in name:
                        continue
                    
                    # Zaman filtresi: Başlamasına SETTINGS["JOIN_UPCOMING_MINS"] dakikadan az kaldıysa katıl
                    starts_at = t.get('startsAt', 0) / 1000 # ms to sec
                    if (starts_at - time.time()) > (SETTINGS.get("JOIN_UPCOMING_MINS", 15) * 60):
                        continue

                    # Katılım işlemi
                    self.client.tournaments.join(t['id'])
                    
                    # Kayıt sistemini güncelle ve zaman damgasını kaydet
                    self.registered_tournaments.add(t['id'])
                    self.last_tournament_join = time.time() 
                    
                    print(f"🏆 [Tournament] Katılındı: {t.get('fullName')}")
                    print(f"🕒 [Tournament] {self.tournament_cooldown / 60} dakika mola başlatıldı.")
                    
                    # Bir turnuvaya katıldıktan sonra döngüyü kır ki aynı anda birden fazla iş yükü oluşmasın
                    break 
                    
        except Exception as e:
            print(f"⚠️ [Tournament] Hata oluştu: {e}")
            
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

    def _find_suitable_target(self):
        self._refresh_bot_pool()
        now = datetime.now()
        roll = random.random()
        target_range = SETTINGS["TIER_HIGH"] if roll < 0.75 else (SETTINGS["TIER_MID"] if roll < 0.95 else SETTINGS["TIER_LOW"])

        random.shuffle(self.bot_pool)
        for bot_id in self.bot_pool[:30]:
            if bot_id in self.blacklist and self.blacklist[bot_id] > now: continue
            rating = self._get_bot_rating(bot_id)
            time.sleep(0.4) 
            if target_range[0] <= rating <= target_range[1]:
                return bot_id, rating
        return None, 0

    def start(self):
        if not self.enabled: return
        print(f"🚀 OxyBullet Hybrid Manager Aktif. (Matchmaking + Tournament)")

        while True:
            try:
                # 1. Turnuva Yönetimi: Katılım kontrolleri ve kayıt işlemleri
                self._manage_tournaments()

                # 2. Güvenlik ve Durum Kontrolü
                # Eğer bir turnuva maçındaysak, matchmaking'i tamamen durdurup 60 sn bekliyoruz.
                if self._is_in_tournament_game():
                    print("⚔️ [Matchmaker] Turnuva maçındayım, matchmaking askıya alındı.")
                    time.sleep(60)
                    continue

                if self._is_stop_triggered():
                    time.sleep(15)
                    continue

                # 3. Klasik Matchmaking Döngüsü
                # Aynı anda kaç oyun oynanabileceği kontrolü
                if len(self.active_games) < SETTINGS["MAX_PARALLEL_GAMES"]:
                    target, target_rating = self._find_suitable_target()
                    
                    if target:
                        # Varyant ve zaman kontrolü belirleme
                        variant = 'chess960' if random.random() < SETTINGS["CHESS960_CHANCE"] else 'standard'
                        tc = random.choice(SETTINGS["TIME_CONTROLS"])
                        t_limit_raw, t_inc = map(float, tc.split('+'))
                        is_rated = SETTINGS["RATED_MODE"] if target_rating >= 1800 else False
                        
                        print(f"[Matchmaker] -> {target} ({target_rating}) davet ediliyor...")
                        self.blacklist[target] = datetime.now() + timedelta(minutes=SETTINGS["BLACKLIST_MINUTES"])
                        
                        self.client.challenges.create(
                            username=target, 
                            rated=is_rated, 
                            variant=variant,
                            clock_limit=int(t_limit_raw * 60), 
                            clock_increment=int(t_inc)
                        )
                        # Bir sonraki davet için güvenli bekleme süresi
                        time.sleep(SETTINGS["SAFETY_LOCK_TIME"])
                    else:
                        # Uygun rakip bulunamadığında kısa bir dinlenme
                        time.sleep(10)
                else:
                    # Kapasite doluysa bekle
                    time.sleep(10)

            except Exception as e:
                # API Limit aşımı (429) veya bağlantı hatası yönetimi
                error_str = str(e)
                if "429" in error_str:
                    print(f"⚠️ [Matchmaker] Hız sınırı aşıldı, {self.wait_timeout}sn bekleniyor.")
                    time.sleep(self.wait_timeout)
                    self.wait_timeout = min(self.wait_timeout * 2, 900)
                else:
                    print(f"⚠️ [Matchmaker] Hata: {error_str}")
                    time.sleep(30)
