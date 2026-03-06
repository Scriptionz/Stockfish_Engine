import time
import random
import itertools
import os
from datetime import datetime, timedelta

# ==========================================================
# ⚙️ MATCHMAKER AYARLARI
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
    "TIME_CONTROLS": [
        "0.5+0", "1+0", "1+1", "2+1",
        "3+0", "3+2", "5+0", "5+3",
        "10+0", "10+5", "15+10",
        "30+0"
    ],
    "CHESS960_CHANCE": 0.10
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

    def _initialize_id(self):
        try:
            self.my_id = self.client.account.get()['id']
            print(f"[Matchmaker] Bağlantı Başarılı. ID: {self.my_id}")
        except: 
            self.my_id = "oxydan"

    def _is_stop_triggered(self):
        """STOP.txt kontrolü yapar."""
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
            ratings = [
                perfs.get('blitz', {}).get('rating', 0),
                perfs.get('bullet', {}).get('rating', 0),
                perfs.get('rapid', {}).get('rating', 0),
                perfs.get('classical', {}).get('rating', 0)
            ]
            return max(ratings) if ratings else 0
        except:
            return 0

    def _refresh_bot_pool(self):
        now = time.time()
        if not self.bot_pool or (now - self.last_pool_update > SETTINGS["POOL_REFRESH_SECONDS"]):
            try:
                stream = self.client.bots.get_online_bots()
                online_bots = list(itertools.islice(stream, 150))
                self.bot_pool = [b.get('id') for b in online_bots if b.get('id') and b.get('id').lower() != self.my_id.lower()]
                random.shuffle(self.bot_pool)
                self.last_pool_update = now
                print(f"[Matchmaker] Havuz yenilendi ({len(self.bot_pool)} bot).")
            except: 
                time.sleep(10)

    def _find_suitable_target(self):
        self._refresh_bot_pool()
        now = datetime.now()

        # Olasılık zarı at
        roll = random.random()
        if roll < 0.75:
            target_range = SETTINGS["TIER_HIGH"]
        elif roll < 0.95:
            target_range = SETTINGS["TIER_MID"]
        else:
            target_range = SETTINGS["TIER_LOW"]

        # Havuzu karıştırıp uygun olan İLK botu bulalım (API'yi yormamak için)
        random.shuffle(self.bot_pool)
        for bot_id in self.bot_pool[:30]: # Max 30 bot tara
            if bot_id in self.blacklist and self.blacklist[bot_id] > now:
                continue
            
            rating = self._get_bot_rating(bot_id)
            # Lichess'i korumak için her rating sorgusu arasına minik bir es ver
            time.sleep(0.5) 

            if target_range[0] <= rating <= target_range[1]:
                return bot_id, rating
        
        return None, 0

    def start(self):
        if not self.enabled: return
        print(f"🚀 Void Matchmaker Aktif. (Max Slot: {SETTINGS['MAX_PARALLEL_GAMES']})")

        while True:
            if self._is_stop_triggered():
                time.sleep(15); continue

            if len(self.active_games) >= SETTINGS["MAX_PARALLEL_GAMES"]:
                time.sleep(10); continue

            try:
                target, target_rating = self._find_suitable_target()
                if not target:
                    time.sleep(5); continue

                variant = 'chess960' if random.random() < SETTINGS["CHESS960_CHANCE"] else 'standard'
                tc = random.choice(SETTINGS["TIME_CONTROLS"])
                t_limit_raw, t_inc = map(float, tc.split('+'))
                
                is_rated = SETTINGS["RATED_MODE"]
                if target_rating < 1800: is_rated = False 

                print(f"[Matchmaker] -> {target} ({target_rating}) Davet ediliyor... [{tc} {variant}]")
                
                self.blacklist[target] = datetime.now() + timedelta(minutes=SETTINGS["BLACKLIST_MINUTES"])
                
                self.client.challenges.create(
                    username=target,
                    rated=is_rated,
                    variant=variant,
                    clock_limit=int(t_limit_raw * 60),
                    clock_increment=int(t_inc)
                )
                
                time.sleep(SETTINGS["SAFETY_LOCK_TIME"]) 

            except Exception as e:
                if "429" in str(e):
                    print(f"⚠️ Rate Limit! {self.wait_timeout}sn bekleme.")
                    time.sleep(self.wait_timeout)
                    self.wait_timeout = min(self.wait_timeout * 2, 900)
                else:
                    print(f"[Matchmaker] Hata: {e}")
                    time.sleep(10)
