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
    
    "GREETING": "Oxydan 9 On The Board!",
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
        # 1. KİTAP KULLANIMI (Sadece varyant standartsa ve yasal hamle varsa)
        if not board.chess960 and os.path.exists(self.book_path):
            try:
                with chess.polyglot.open_reader(self.book_path) as reader:
                    entries = list(reader.find_all(board))
                    if entries:
                        # Hamleleri karıştır ve ilk yasal olanı yap
                        shuffled_entries = list(entries)
                        random.shuffle(shuffled_entries)
                        for entry in shuffled_entries:
                            if entry.move in board.legal_moves:
                                return entry.move
            except Exception as e:
                print(f"📖 Kitap Hatası: {e}")

        # 2. TABLEBASE (Oyun sonu 7 taş ve altı)
        if not board.chess960 and len(board.piece_map()) <= SETTINGS["TABLEBASE_PIECE_LIMIT"]:
            try:
                fen = board.fen()
                r = requests.get(f"https://tablebase.lichess.ovh/standard?fen={fen}", timeout=0.5)
                if r.status_code == 200:
                    data = r.json()
                    if data.get("moves"):
                        best_table_move = chess.Move.from_uci(data["moves"][0]["uci"])
                        if best_table_move in board.legal_moves:
                            return best_table_move
            except: pass

        # 3. MOTOR (Ethereal)
        engine = None
        try:
            engine = self.engine_pool.get()
            my_time = self.to_seconds(wtime if board.turn == chess.WHITE else btime)
            my_inc = self.to_seconds(winc if board.turn == chess.WHITE else binc)
            think_time = self.calculate_smart_time(my_time, my_inc, board)
            
            result = engine.play(board, chess.engine.Limit(time=think_time))
            if result.move and result.move in board.legal_moves:
                return result.move
        except Exception as e:
            print(f"🚨 Motor Hatası: {e}")
        finally:
            if engine: self.engine_pool.put(engine)

        return next(iter(board.legal_moves))
            
def handle_game(client, game_id, bot, my_id, mm):
    """Oyun mantığını yöneten ana fonksiyon. mm parametresi eklendi."""
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
                
                # DÜZELTME: Takibi Matchmaker nesnesi üzerinden yapıyoruz
                if mm:
                    mm.opponent_tracker[opp_id] = mm.opponent_tracker.get(opp_id, 0) + 1
                
                curr_state = state['state']
            elif state['type'] == 'gameState':
                curr_state = state
            else:
                continue

            # Hamleleri güncelle
            moves = curr_state.get('moves', "").split()
            if len(moves) > last_move_count:
                for m in moves[last_move_count:]:
                    board.push_uci(m)
                last_move_count = len(moves)

            if curr_state.get('status') in ['mate', 'resign', 'draw', 'outoftime', 'aborted', 'stalemate']:
                break

            # Hamle sırası kontrolü
            if board.turn == my_color and not board.is_game_over():
                move = bot.get_best_move(
                    board, 
                    curr_state.get('wtime'), 
                    curr_state.get('btime'), 
                    curr_state.get('winc'), 
                    curr_state.get('binc')
                )
                
                if move:
                    for _ in range(3):
                        try:
                            client.bots.make_move(game_id, move.uci())
                            break
                        except Exception:
                            time.sleep(0.05)

    except Exception as e:
        print(f"🚨 Oyun Hatası ({game_id}): {e}", flush=True)

def handle_game_wrapper(client, game_id, bot, my_id, active_games, mm):
    """Thread başlatıcı. mm parametresi handle_game'e aktarılıyor."""
    try: 
        handle_game(client, game_id, bot, my_id, mm) # mm buraya eklendi!
    finally: 
        active_games.discard(game_id)

def main():
    """Botun ana döngüsü ve Matchmaker entegrasyonu."""
    start_time = time.time()
    session = berserk.TokenSession(SETTINGS["TOKEN"])
    client = berserk.Client(session=session)
    
    try:
        with open("config.yml", "r") as f: 
            config = yaml.safe_load(f)
        my_id = client.account.get()['id']
    except Exception as e:
        print(f"Bağlantı Hatası: {e}"); return

    bot = OxydanAegisV4(SETTINGS["ENGINE_PATH"], uci_options=config.get('engine', {}).get('uci_options', {}))
    active_games = set() 
    
    # lichess-bot.py içerisindeki Matchmaker başlatma kısmını bul ve şöyle düzelt:
    
    mm = None
    if config.get("matchmaking"):
        # Buraya token parametresini ekliyoruz
        mm = Matchmaker(client, config, active_games, token=SETTINGS["TOKEN"])
        threading.Thread(target=mm.start, daemon=True).start()

    print(f"🔥 Oxydan 9 Hazır. ID: {my_id}", flush=True)

    while True:
        try:
            for event in client.bots.stream_incoming_events():
                cur_elapsed = time.time() - start_time
                should_stop = os.path.exists("STOP.txt") or cur_elapsed > SETTINGS["MAX_TOTAL_RUNTIME"]
                close_to_end = cur_elapsed > (SETTINGS["MAX_TOTAL_RUNTIME"] - (SETTINGS["STOP_ACCEPTING_MINS"] * 60))

                if event['type'] == 'challenge':
                    ch_id = event['challenge']['id']
                    
                    # DÜZELTME: mm nesnesi üzerinden metod çağrısı
                    accept, reason = True, 'policy'
                    if mm:
                        accept, reason = mm.is_challenge_acceptable(event['challenge'])
                    
                    if not should_stop and not close_to_end and len(active_games) < SETTINGS["MAX_PARALLEL_GAMES"] and accept:
                        client.challenges.accept(ch_id)
                    else:
                        client.challenges.decline(ch_id, reason='later' if accept else 'policy')
                        if should_stop and len(active_games) == 0: os._exit(0)

                elif event['type'] == 'gameStart':
                    game_id = event['game']['id']
                    if game_id not in active_games:
                        active_games.add(game_id)
                        threading.Thread(
                            target=handle_game_wrapper, 
                            args=(client, game_id, bot, my_id, active_games, mm), # Tam argüman listesi
                            daemon=True
                        ).start()

        except Exception as e:
            print(f"⚠️ Akış koptu: {e}", flush=True)
            time.sleep(5)

if __name__ == "__main__":
    main()
