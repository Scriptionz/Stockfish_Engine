import time
import random
import itertools
import os
from datetime import datetime, timedelta

# ==========================================================
# ⚙️ MATCHMAKER AYARLARI (Buradan yönetebilirsin)
# ==========================================================
SETTINGS = {
    "RATED_MODE": True,          # True: Puanlı, False: Puansız (Test için False kalmalı)
    "MAX_PARALLEL_GAMES": 2,     # Aynı anda kaç maç yapılsın? (GitHub için 1 önerilir)
    "MIN_RATING": 2250,          # Rakip minimum kaç elo olsun?
    "MAX_RATING": 4000,          # Rakip maksimum kaç elo olsun?
    "SAFETY_LOCK_TIME": 60,      # Davet attıktan sonra kaç saniye dondurulsun? (Beton Fren)
    "LOW_ELO_THRESHOLD": 2250,
    "STOP_FILE": "STOP.txt",     # Durdurma dosyası adı
    "TIME_CONTROLS": ["1+0", "1+1", "2+1",                  # Bullet
        "3+0", "3+2", "5+0", "5+3",            # Blitz
        "10+0", "10+5", "15+10",               # Rapid
        "30+0"], # Rastgele seçilecek süreler
    "POOL_REFRESH_SECONDS": 1800, # Bot listesi kaç saniyede bir güncellensin?
    "BLACKLIST_MINUTES": 30      # Reddeden veya maç yapılan botu kaç dk engelle?
}
# ==========================================================

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
        """Botun kendi ID'sini doğrular."""
        try:
            self.my_id = self.client.account.get()['id']
            print(f"[Matchmaker] Bağlantı Başarılı. ID: {self.my_id}")
        except: 
            self.my_id = "oxydan"

    def _refresh_bot_pool(self):
        """Online bot listesini çeker ve karıştırır."""
        now = time.time()
        if not self.bot_pool or (now - self.last_pool_update > SETTINGS["POOL_REFRESH_SECONDS"]):
            try:
                stream = self.client.bots.get_online_bots()
                online_bots = list(itertools.islice(stream, 50))
                self.bot_pool = [b.get('id') for b in online_bots if b.get('id') and b.get('id').lower() != self.my_id.lower()]
                random.shuffle(self.bot_pool)
                self.last_pool_update = now
                print(f"[Matchmaker] Bot havuzu güncellendi: {len(self.bot_pool)} bot bulundu.")
            except: 
                time.sleep(10)

    def _get_bot_rating(self, bot_id):
        """Botun en yüksek ratingini (Blitz, Bullet veya Rapid) döndürür."""
        try:
            user_data = self.client.users.get_public_data(bot_id)
            perfs = user_data.get('perfs', {})
            # Mevcut ratingleri topla, yoksa 0 say
            ratings = [
                perfs.get('blitz', {}).get('rating', 0),
                perfs.get('bullet', {}).get('rating', 0),
                perfs.get('rapid', {}).get('rating', 0)
            ]
            return max(ratings) if ratings else 0
        except Exception:
            return 0

    def _is_stop_triggered(self):
        """STOP.txt kontrolü yapar ve aktif maç yoksa sistemi tamamen kapatır."""
        if os.path.exists(SETTINGS["STOP_FILE"]):
            if len(self.active_games) == 0:
                print(f"🏁 [Matchmaker] Maç kalmadı. {SETTINGS['STOP_FILE']} gereği sistem kapatılıyor.")
                os._exit(0)  # GitHub Actions sürecini tamamen öldürür
            return True
        return False

    def _find_suitable_target(self):
        self._refresh_bot_pool()
        now = datetime.now()

        # Bot havuzunu çok hızlı tarama, Lichess bunu spam sayar
        for candidate in self.bot_pool[:10]: # 20 yerine 10 bot yeterli
            if candidate in self.blacklist and self.blacklist[candidate] > now:
                continue
            
            # İstekler arasına nefes payı koy
            time.sleep(3) 
            
            try:
                # Veriyi BİR KEZ çekiyoruz
                user_data = self.client.users.get_public_data(candidate)
                if user_data.get('tosViolation') or user_data.get('disabled'):
                    continue

                perfs = user_data.get('perfs', {})
                ratings = [perfs.get(c, {}).get('rating', 0) for c in ['blitz', 'bullet', 'rapid']]
                max_r = max(ratings) if ratings else 0

                if SETTINGS["MIN_RATING"] <= max_r <= SETTINGS["MAX_RATING"]:
                    # Hedef botun şu an maç yapıp yapmadığını kontrol et (Opsiyonel ama iyi olur)
                    if user_data.get('playing'):
                         continue
                    return candidate, max_r # Rating'i de beraber dön
                else:
                    self.blacklist[candidate] = now + timedelta(hours=12)
            except Exception as e:
                if "429" in str(e): raise e # Rate limit hatasını yukarı fırlat
                continue
        return None, 0

   def start(self):
        if not self.enabled: return
        print(f"🚀 Oxydan Matchmaker Aktif. (Max Slot: {SETTINGS['MAX_PARALLEL_GAMES']})")

        while True:
            # --- 1. STOP KONTROLÜ ---
            if self._is_stop_triggered():
                if len(self.active_games) == 0:
                    os._exit(0)
                time.sleep(30)
                continue

            # --- 2. SLOT KONTROLÜ ---
            if len(self.active_games) >= SETTINGS["MAX_PARALLEL_GAMES"]:
                time.sleep(15)
                continue

            try:
                # --- 3. RAKİP BULMA (Yeni Tuple Mantığı) ---
                target, target_rating = self._find_suitable_target() 
                if not target:
                    time.sleep(30) # Uygun rakip yoksa API'yi yormadan bekle
                    continue

                # --- 4. STRATEJİ BELİRLEME ---
                if target_rating < SETTINGS["LOW_ELO_THRESHOLD"]:
                    is_rated = False
                    tc = random.choice(["1+0", "1+1", "3+0"])
                else:
                    is_rated = SETTINGS["RATED_MODE"]
                    tc = random.choice(SETTINGS["TIME_CONTROLS"])

                t_limit, t_inc = map(int, tc.split('+'))

                # --- 5. MEYDAN OKUMA ---
                print(f"[Matchmaker] -> {target} ({tc}) Davet ediliyor... (Rating: {target_rating})")
                
                # Daveti atmadan hemen önce blacklist'e al (Çift daveti önler)
                self.blacklist[target] = datetime.now() + timedelta(minutes=SETTINGS["BLACKLIST_MINUTES"])
                
                self.client.challenges.create(
                    username=target,
                    rated=is_rated,
                    clock_limit=t_limit * 60,
                    clock_increment=t_inc
                )
                
                # Başarılı işlemde hata zaman aşımını sıfırla
                self.wait_timeout = 120
                print(f"[Matchmaker] ✅ Davet gitti. {SETTINGS['SAFETY_LOCK_TIME']}sn Kilit.")
                time.sleep(SETTINGS["SAFETY_LOCK_TIME"])

            except Exception as e:
                err_msg = str(e)
                if "429" in err_msg:
                    print(f"🚨 [Matchmaker] RATE LIMIT! {self.wait_timeout}sn tam sessizlik...")
                    time.sleep(self.wait_timeout)
                    # Hata devam ederse bekleme süresini katla
                    self.wait_timeout = min(self.wait_timeout * 2, 3600) 
                elif "Not found" in err_msg:
                    print(f"⚠️ [Matchmaker] Bot bulunamadı veya davet kapalı. Pas geçiliyor.")
                    time.sleep(10)
                else:
                    print(f"[Matchmaker] Beklenmedik Hata: {e}")
                    time.sleep(30)
