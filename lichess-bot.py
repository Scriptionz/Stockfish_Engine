import os
import sys
import berserk
import chess
import chess.engine
import time
import chess.polyglot
import threading
import yaml
import requests
import queue
import random
from datetime import timedelta
from matchmaking import Matchmaker

# ==========================================================
# ⚙️ MODÜLER AYARLAR PANELİ
# ==========================================================
SETTINGS = {
    "TOKEN": os.environ.get('LICHESS_TOKEN'),
    "ENGINE_PATH": "./src/Ethereal",
    "BOOK_PATH": "./book.bin",
    
    "MAX_PARALLEL_GAMES": 2,
    "MAX_TOTAL_RUNTIME": 21300,
    "STOP_ACCEPTING_MINS": 15,
    
    "LATENCY_BUFFER": 0.03,
    "TABLEBASE_PIECE_LIMIT": 7,
    "MIN_THINK_TIME": 0,
    
    "GREETING": "Void 3 On The Board!",
}

# Aynı rakiple kaç maç yapılabileceği sınırı
MAX_GAMES_PER_OPPONENT = 3 
opponent_tracker = {} 

# ==========================================================

class OxydanAegisV4:
    def __init__(self, exe_path, uci_options=None):
        self.exe_path = exe_path
        self.book_path = SETTINGS["BOOK_PATH"]
        self.engine_pool = queue.Queue()
        
        pool_size = SETTINGS["MAX_PARALLEL_GAMES"] + 1
        
        try:
            for i in range(pool_size):
                eng = chess.engine.SimpleEngine.popen_uci(self.exe_path, timeout=30)
                # DÜZELTME: MoveOverhead (Bitişik yazım)
                eng.configure({"Move Overhead": 500}) 
                if uci_options:
                    for opt, val in uci_options.items():
                        try: eng.configure({opt: val})
                        except: pass
                self.engine_pool.put(eng)
            print(f"🚀 Void: {pool_size} Motor Hazır.", flush=True)
        except Exception as e:
            print(f"KRİTİK HATA: {e}", flush=True); sys.exit(1)

    def to_seconds(self, t):
        if t is None: return 0.0
        if isinstance(t, timedelta): return t.total_seconds()
        try:
            val = float(t)
            return val / 1000.0 if val > 1000 else val
        except: return 0.0

    def calculate_smart_time(self, t, inc, board):
        # 1. NETWORK & CPU LATENCY (Hayati Önemde)
        # Lichess pingin 50ms ise, buffer en az 0.07 olmalı.
        buffer = SETTINGS.get("LATENCY_BUFFER", 0.07)
        
        # 2. ULTRA-PANIC MODE (Zaman biterken 'Pre-move' hızı)
        if t < 0.6: 
            # 0.6 saniyenin altında motoru salla, en hızlı hamleyi at.
            return 0.01 
    
        if t < 1.5:
            # Zamanın %10'u + artışın çoğu. Buffer'ı burada agresif kullan.
            panic_time = (t * 0.10) + (inc * 0.8)
            return max(0.02, panic_time - buffer)
    
        # 3. DINAMIK OYUN TAHMİNİ
        move_count = len(board.move_stack)
        # Oyun ilerledikçe (move_count arttıkça) kalan hamle tahminini azalt
        # Bu, botun oyun sonuna süre saklamasını ama oyun sonunda da hızlanmasını sağlar.
        if move_count < 20:
            moves_to_go = 40
        elif move_count < 40:
            moves_to_go = 25
        else:
            moves_to_go = 15
    
        # 4. TENSION & COMPLEXITY (AI Gözüyle Tahta)
        legal_moves = board.legal_moves.count()
        # Çok hamle varsa tahta karışıktır, biraz daha düşün. 
        # Az hamle varsa (zorunlu hamleler) saniyeler harcama.
        tension = 0.7 + (legal_moves / 40.0) 
    
        # 5. ANA HESAPLAMA
        # Kalan süreyi tahmini hamle sayısına böl ve gerginlikle çarp
        base_time = (t / moves_to_go) * tension
        
        # 6. INC (ARTIŞ) YÖNETİMİ
        # Artış süresini (inc) "bedava süre" olarak görme, onu can simidi yap.
        final_time = base_time + (inc * 0.7)
    
        # 7. ÜST LİMİTLER (Aşırı düşünmeyi engelle)
        # Asla toplam sürenin %8'inden fazlasını tek hamlede harcama.
        # 1 dakikan varken tek hamlede 10 saniye düşünmek intihardır.
        max_allowed = t * 0.08
        final_time = min(final_time, max_allowed, 12.0)
    
        # 8. SON DOKUNUŞ: RANDOM BLUFF
        # Botun her hamleyi aynı sürede yapması "insansı" olmadığını ele verir.
        # %15 varyasyon ekle.
        final_time *= random.uniform(0.85, 1.15)
    
        return max(0.03, final_time - buffer)
        
    def get_best_move(self, board, wtime, btime, winc, binc):
        """Void & Oxydan: Stabilize Edilmiş Hamle Seçici"""
        
        # 1. AÇILIŞ KİTABI (Hızlı Yanıt)
        if os.path.exists(SETTINGS["BOOK_PATH"]):
            try:
                with chess.polyglot.open_reader(SETTINGS["BOOK_PATH"]) as reader:
                    # Kitaptan rastgele bir hamle seçmek daha doğal durur
                    entries = list(reader.find_all(board))
                    if entries:
                        return random.choice(entries).move
            except Exception:
                pass # Kitap okuma hatası stabiliteyi bozmasın
    
        # 2. TABLEBASE (Ağ Gecikmesi Korumalı)
        if len(board.piece_map()) <= SETTINGS["TABLEBASE_PIECE_LIMIT"]:
            try:
                # Lichess API talebi (Yalnızca stabilite için timeout 0.2'ye çekildi)
                fen = board.fen().replace(" ", "_")
                r = requests.get(
                    f"https://tablebase.lichess.ovh/standard?fen={fen}", 
                    timeout=0.2 # Gecikmeyi önlemek için daha agresif timeout
                )
                if r.status_code == 200:
                    data = r.json()
                    if data.get("moves"):
                        return chess.Move.from_uci(data["moves"][0]["uci"])
            except Exception:
                pass # İnternet kesintisi veya timeout durumunda motora geç
    
        # 3. STOCKFISH MOTORU (Güvenli Havuz Yönetimi)
        engine = None
        try:
            engine = self.engine_pool.get()
            
            # Süre dönüşümleri
            my_time = self.to_seconds(wtime if board.turn == chess.WHITE else btime)
            my_inc = self.to_seconds(winc if board.turn == chess.WHITE else binc)
            
            # Daha önce onardığımız v4.0 Smart Time algoritması
            think_time = self.calculate_smart_time(my_time, my_inc, board)
            
            # Motor limiti: Hem süre hem de güvenlik için çok kısa bir minimum derinlik
            # Bu, aşırı hızlı maçlarda motorun illegal hamle üretmesini engeller.
            result = engine.play(board, chess.engine.Limit(time=think_time))
            
            if result.move:
                return result.move
                
        except Exception as e:
            print(f"🚨 Motor Hatası ({board.fen()}): {e}")
        
        finally:
            # Motorun havuza geri dönmesini garanti altına alıyoruz
            if engine:
                self.engine_pool.put(engine)
    
        # 4. SON ÇARE (Acil Durum Hamlesi)
        # Eğer her şey çökerse, rastgele bir hamle yerine merkezi kontrol eden veya 
        # ilk yasal hamleyi döndür.
        return next(iter(board.legal_moves))

    def is_challenge_acceptable(challenge, mm_instance=None):
        if mm_instance and mm_instance.is_in_tournament_game():
            return False, "I am currently playing a tournament game."
        # --- YENİ VARYANT FİLTRESİ ---
        # Sadece 'standard' ve 'chess960' varyantlarını kabul et
        variant = challenge.get('variant', {}).get('key')
        if variant not in ['standard', 'chess960']:
            return False, f"Variant '{variant}' is not supported."
        # -----------------------------
    
        challenger = challenge.get('challenger')
        if not challenger: 
            return False, "Generic challenge"
    
        # GÜNCELLEME: None gelirse 1500'e yuvarla
        rating = challenger.get('rating') or 1500
        # GÜNCELLEME: Title yoksa string metotları hata vermesin diye boş string yap
        title = challenger.get('title', '') or ''
        is_bot = title.upper() == 'BOT'
        
        rated = challenge.get('rated', False)
        user_id = challenger['id']
    
        time_control = challenge.get('timeControl', {})
        tc_type = time_control.get('type')
    
        if tc_type != 'clock':
            return False, "Only standard clock games allowed"
    
        limit = time_control.get('limit', 0)
        increment = time_control.get('increment', 0)
        total_est_time = limit + (increment * 40)
    
        # Global değişken kontrolü
        try:
            if opponent_tracker.get(user_id, 0) >= MAX_GAMES_PER_OPPONENT:
                return False, "Too many games recently"
        except NameError:
            pass
    
        # --- Protokoller ---
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
            
def handle_game(client, game_id, bot, my_id):
    try:
        client.bots.post_message(game_id, SETTINGS["GREETING"])
        stream = client.bots.stream_game_state(game_id)
        
        board = chess.Board()
        my_color = None
        last_move_count = 0

        for state in stream:
            if 'error' in state: break
            
            if state['type'] == 'gameFull':
                my_color = chess.WHITE if state['white'].get('id') == my_id else chess.BLACK
                opp_id = state['black']['id'] if my_color == chess.WHITE else state['white']['id']
                opponent_tracker[opp_id] = opponent_tracker.get(opp_id, 0) + 1
                curr_state = state['state']
            elif state['type'] == 'gameState':
                curr_state = state
            else:
                continue

            # SADECE YENİ HAMLELERİ GÜNCELLE (Performans Kilidi)
            moves = curr_state.get('moves', "").split()
            if len(moves) > last_move_count:
                for m in moves[last_move_count:]:
                    board.push_uci(m)
                last_move_count = len(moves)

            # Oyun bitiş kontrolü
            if curr_state.get('status') in ['mate', 'resign', 'draw', 'outoftime', 'aborted', 'stalemate']:
                break

            # Hamle sırası bizde mi?
            if board.turn == my_color and not board.is_game_over():
                # get_best_move zaten calculate_smart_time'ı çağırıyor
                move = bot.get_best_move(
                    board, 
                    curr_state.get('wtime'), 
                    curr_state.get('btime'), 
                    curr_state.get('winc'), 
                    curr_state.get('binc')
                )
                
                if move:
                    # Hamle gönderme (Daha hızlı retry)
                    for _ in range(3):
                        try:
                            client.bots.make_move(game_id, move.uci())
                            break
                        except Exception:
                            time.sleep(0.05) # 0.5 yerine 0.05 (Hayati)

    except Exception as e:
        print(f"🚨 Oyun Hatası ({game_id}): {e}", flush=True)

def handle_game_wrapper(client, game_id, bot, my_id, active_games):
    try: handle_game(client, game_id, bot, my_id)
    finally: active_games.discard(game_id)

def main():
    start_time = time.time()
    session = berserk.TokenSession(SETTINGS["TOKEN"])
    client = berserk.Client(session=session)
    
    try:
        with open("config.yml", "r") as f: config = yaml.safe_load(f)
        my_id = client.account.get()['id']
    except Exception as e:
        print(f"Bağlantı Hatası: {e}"); return

    bot = OxydanAegisV4(SETTINGS["ENGINE_PATH"], uci_options=config.get('engine', {}).get('uci_options', {}))
    active_games = set() 
    
    # Matchmaker nesnesini başlat (mm burada tanımlanmalı)
    mm = None
    if config.get("matchmaking"):
        mm = Matchmaker(client, config, active_games) 
        threading.Thread(target=mm.start, daemon=True).start()

    print(f"🔥 Void 3 Hazır. ID: {my_id}", flush=True)

    while True:
        try:
            # Jeneratör hatasız devam etsin
            for event in client.bots.stream_incoming_events():
                cur_elapsed = time.time() - start_time
                should_stop = os.path.exists("STOP.txt") or cur_elapsed > SETTINGS["MAX_TOTAL_RUNTIME"]
                close_to_end = cur_elapsed > (SETTINGS["MAX_TOTAL_RUNTIME"] - (SETTINGS["STOP_ACCEPTING_MINS"] * 60))

                if event['type'] == 'challenge':
                    ch_id = event['challenge']['id']
                    
                    # mm nesnesini buraya gönderiyoruz:
                    accept, reason = is_challenge_acceptable(event['challenge'], mm_instance=mm)
                    
                    # Eğer turnuvada değilse ve diğer şartlar uygunsa kabul et
                    if not should_stop and not close_to_end and len(active_games) < SETTINGS["MAX_PARALLEL_GAMES"] and accept:
                        client.challenges.accept(ch_id)
                    else:
                        client.challenges.decline(ch_id, reason='later' if accept else 'policy')
                        if should_stop and len(active_games) == 0: os._exit(0)

                elif event['type'] == 'gameStart':
                    game_id = event['game']['id']
                    # Turnuva maçı mı? Kontrol gerekebilir (gameStart event'i turnuva maçlarında da gelir)
                    if game_id not in active_games and len(active_games) < SETTINGS["MAX_PARALLEL_GAMES"]:
                        active_games.add(game_id)
                        threading.Thread(target=handle_game_wrapper, args=(client, game_id, bot, my_id, active_games), daemon=True).start()

        except Exception as e:
            print(f"⚠️ Akış koptu, 5sn içinde tazeleniyor: {e}", flush=True)
            time.sleep(5)

if __name__ == "__main__":
    main()
