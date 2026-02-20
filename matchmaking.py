import time
import random
import itertools
import os
from datetime import datetime, timedelta

# ==========================================================
# ⚙️ MATCHMAKER AYARLARI (Buradan yönetebilirsin)
# ==========================================================
SETTINGS = {
    "RATED_MODE": False,          # True: Puanlı, False: Puansız (Test için False kalmalı)
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
        """Ayarlara uygun rakibi seçer."""
        self._refresh_bot_pool()
        now = datetime.now()

        for candidate in self.bot_pool[:20]: # İlk 20 botu hızlıca tara
            if candidate in self.blacklist and self.blacklist[candidate] > now:
                continue
            time.sleep(2)
            
            try:
                user_data = self.client.users.get_public_data(candidate)
                perfs = user_data.get('perfs', {})
                # En yüksek rating hangisiyse onu baz al
                max_r = max([perfs.get(c, {}).get('rating', 0) for c in ['blitz', 'bullet', 'rapid']] or [0])

                if SETTINGS["MIN_RATING"] <= max_r <= SETTINGS["MAX_RATING"]:
                    return candidate
                else:
                    # Kriter dışı botu 12 saat engelle
                    self.blacklist[candidate] = now + timedelta(hours=12)
            except: 
                continue
        return None

    def start(self):
        if not self.enabled: return
        print(f"🚀 Oxydan Matchmaker Aktif. (Max Slot: {SETTINGS['MAX_PARALLEL_GAMES']})")

        while True:
            # --- 1. AKILLI STOP KONTROLÜ (Düzeltildi) ---
            if self._is_stop_triggered():
                active_count = len(self.active_games)
                if active_count == 0:
                    print(f"🏁 Maç kalmadı. {SETTINGS['STOP_FILE']} gereği sistem tamamen kapatılıyor.")
                    os._exit(0)  # Süreci kesin olarak bitirir
                else:
                    print(f"⏳ STOP algılandı! Mevcut {active_count} maçın bitmesi bekleniyor... Yeni davet atılmayacak.")
                    time.sleep(30)
                    continue # Yeni maç arama adımını atla, döngü başına dön

            # --- 2. Maç Sayısı Kontrolü ---
            if len(self.active_games) >= SETTINGS["MAX_PARALLEL_GAMES"]:
                time.sleep(15)
                continue

            try:
                # --- 3. Rakip Bulma ---
                target = self._find_suitable_target()
                if not target:
                    time.sleep(20)
                    continue

                # --- 4. ELO BAZLI STRATEJİ (2000 ELO Altı Düzenlemesi) ---
                target_rating = self._get_bot_rating(target)
                
                if target_rating < SETTINGS["LOW_ELO_THRESHOLD"]:
                    # 2000 Altı: Her zaman PUANSIZ ve Hızlı Tempo
                    is_rated = False
                    tc = random.choice(["1+0", "1+1", "2+1", "3+0", "5+0"])
                    print(f"🎯 Düşük ELO ({target_rating}): Puansız ve Hızlı Tempo seçildi.")
                else:
                    # 2000 Üstü: Normal Ayarlar
                    is_rated = SETTINGS["RATED_MODE"]
                    tc = random.choice(SETTINGS["TIME_CONTROLS"])

                t_limit, t_inc = map(int, tc.split('+'))

                # --- 5. Meydan Okuma ---
                print(f"[Matchmaker] -> {target} ({tc}) Davet ediliyor... (Rated: {is_rated})")
                self.blacklist[target] = datetime.now() + timedelta(minutes=SETTINGS["BLACKLIST_MINUTES"])
                
                self.client.challenges.create(
                    username=target,
                    rated=is_rated,
                    clock_limit=t_limit * 60,
                    clock_increment=t_inc
                )
                
                # --- 6. Güvenlik Kilidi ---
                print(f"[Matchmaker] ✅ Davet gitti. {SETTINGS['SAFETY_LOCK_TIME']}sn GÜVENLİK KİLİDİ aktif.")
                time.sleep(SETTINGS["SAFETY_LOCK_TIME"]) 

            except Exception as e:
                if "429" in str(e):
                    print(f"⚠️ [Matchmaker] Lichess Rate Limit uyarısı! {self.wait_timeout} saniye boyunca tüm istekler durduruluyor...")
                    time.sleep(self.wait_timeout)
                    
                    # Hata devam ederse bir sonraki bekleme süresini iki katına çıkar (Maksimum 1 saat olsun)
                    self.wait_timeout = min(self.wait_timeout * 2, 3600) 
                else:
                    print(f"[Matchmaker] Hata: {e}")
                    # Normal hatalarda bekleme süresini sıfırlama, ama 30 saniye bekle
                    time.sleep(30)
                    
                continue
            self.wait_timeout = 120
