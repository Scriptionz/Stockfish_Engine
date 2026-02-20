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
from datetime import timedelta
from matchmaking import Matchmaker

# ==========================================================
# ⚙️ MODÜLER AYARLAR PANELİ (Burayı Değiştirmeniz Yeterli)
# ==========================================================
SETTINGS = {
    "TOKEN": os.environ.get('LICHESS_TOKEN'),
    "ENGINE_PATH": "./src/Ethereal",
    "BOOK_PATH": "./book.bin",
    
    # --- OYUN LİMİTLERİ ---
    "MAX_PARALLEL_GAMES": 2,      # Aynı anda oynanacak maç sayısı
    "MAX_TOTAL_RUNTIME": 21300,   # Toplam çalışma süresi (5 saat 55 dk)
    "STOP_ACCEPTING_MINS": 15,    # Kapanışa kaç dk kala yeni maç almasın?
    
    # --- MOTOR VE ZAMAN YÖNETİMİ ---
    "LATENCY_BUFFER": 0.8,       # Saniye cinsinden ağ gecikme payı (150ms)
    "TABLEBASE_PIECE_LIMIT": 7,   # Kaç taş kalınca tablebase'e sorsun? (6 güvenlidir)
    "MIN_THINK_TIME": 0.05,       # En az düşünme süresi
    
    # --- MESAJLAR ---
    "GREETING": "Void v1 Active. System stabilized.",
}
# ==========================================================

class OxydanAegisV4:
    def __init__(self, exe_path, uci_options=None):
        self.exe_path = exe_path
        self.book_path = SETTINGS["BOOK_PATH"]
        self.engine_pool = queue.Queue()
        self.session = requests.Session() # Bağlantıyı açık tutar (HIZ)
        
        pool_size = SETTINGS["MAX_PARALLEL_GAMES"] + 1
        
        try:
            for i in range(pool_size):
                eng = chess.engine.SimpleEngine.popen_uci(self.exe_path, timeout=30)
                # Kritik UCI Ayarı: Motorun kendi iç gecikme payı
                eng.configure({"Move Overhead": 500}) 
                if uci_options:
                    for opt, val in uci_options.items():
                        try: eng.configure({opt: val})
                        except: pass
                self.engine_pool.put(eng)
            print(f"🚀 Oxydan v7: {pool_size} Motor Ünitesi Hazır.", flush=True)
        except Exception as e:
            print(f"KRİTİK HATA: {e}"); sys.exit(1)

    def to_seconds(self, t):
        if t is None: return 0.0
        if isinstance(t, timedelta): return t.total_seconds()
        try:
            val = float(t)
            return val / 1000.0 if val > 1000 else val
        except: return 0.0

    def calculate_smart_time(self, t, inc, board):
        if t < 1.0: return 0.02 
        if t < 3.0: return 0.05
        
        mtg = 40 if t > 600 else (35 if t > 180 else 30)
        base_budget = (t / mtg) + (inc * 0.7) 
        
        legal_moves = board.legal_moves.count()
        complexity = 1.2 if legal_moves > 40 else (0.8 if legal_moves < 10 else 1.0)
        
        target_time = base_budget * complexity
        final_time = target_time - SETTINGS.get("LATENCY_BUFFER", 0.5)
        
        if t < 15.0: final_time = min(final_time, t / 20)
        return max(SETTINGS["MIN_THINK_TIME"], final_time)

    def get_best_move(self, board, wtime, btime, winc, binc):
        # 1. KİTAP
        if os.path.exists(self.book_path):
            try:
                with chess.polyglot.open_reader(self.book_path) as reader:
                    entry = reader.get(board)
                    if entry: return entry.move
            except: pass

        # 2. SYZYGY (Session kullanarak hızlı sorgu)
        my_time = self.to_seconds(wtime if board.turn == chess.WHITE else btime)
        if my_time > 15 and len(board.piece_map()) <= SETTINGS["TABLEBASE_PIECE_LIMIT"]:
            try:
                fen = board.fen().replace(" ", "_")
                r = self.session.get(f"https://tablebase.lichess.ovh/standard?fen={fen}", timeout=0.2)
                if r.status_code == 200:
                    data = r.json()
                    if data.get("moves"):
                        return chess.Move.from_uci(data["moves"][0]["uci"])
            except: pass

        # 3. MOTOR
        engine = self.engine_pool.get()
        try:
            my_inc = self.to_seconds(winc if board.turn == chess.WHITE else binc)
            think_time = self.calculate_smart_time(my_time, my_inc, board)
            result = engine.play(board, chess.engine.Limit(time=think_time))
            return result.move
        finally:
            self.engine_pool.put(engine)

def handle_game(client, game_id, bot, my_id):
    try:
        client.bots.post_message(game_id, SETTINGS["GREETING"])
        stream = client.bots.stream_game_state(game_id)
        board = chess.Board() 
        last_move_count = 0
        my_color = None

        for state in stream:
            if 'error' in state: break
            
            if state['type'] == 'gameFull':
                my_color = chess.WHITE if state['white'].get('id') == my_id else chess.BLACK
                curr_state = state['state']
            elif state['type'] == 'gameState':
                curr_state = state
            else: continue

            # --- INCREMENTAL UPDATE (CPU ve Zaman Dostu) ---
            moves = curr_state.get('moves', "").split()
            if len(moves) > last_move_count:
                for i in range(last_move_count, len(moves)):
                    board.push_uci(moves[i])
                last_move_count = len(moves)

            if curr_state.get('status') in ['mate', 'resign', 'draw', 'outoftime', 'aborted', 'stalemate']:
                break

            # Hamle Karar ve Gönderim Mekanizması
            if board.turn == my_color and not board.is_game_over():
                wtime, btime = curr_state.get('wtime'), curr_state.get('btime')
                winc, binc = curr_state.get('winc'), curr_state.get('binc')
                
                move = bot.get_best_move(board, wtime, btime, winc, binc)
                
                if move:
                    # Lichess gecikmelerine karşı 2 denemeli gönderim (Hızlı Premove Koruması)
                    for attempt in range(2):
                        try:
                            client.bots.make_move(game_id, move.uci())
                            break 
                        except Exception as e:
                            if "Not your turn" in str(e):
                                time.sleep(0.05) # 50ms bekle ve tekrar dene
                            else:
                                print(f"⚠️ Hamle gönderilemedi: {e}")
                                break
    except Exception as e:
        print(f"🚨 Oyun Hatası ({game_id}): {e}", flush=True)

def handle_game_wrapper(client, game_id, bot, my_id, active_games):
    try:
        handle_game(client, game_id, bot, my_id)
    finally:
        active_games.discard(game_id)
        print(f"✅ [{game_id}] Bitti. Kalan Slot: {len(active_games)}/{SETTINGS['MAX_PARALLEL_GAMES']}", flush=True)

def main():
    start_time = time.time()
    
    try:
        with open("config.yml", "r") as f:
            config = yaml.safe_load(f)
    except:
        print("HATA: config.yml okunamadı."); return

    session = berserk.TokenSession(SETTINGS["TOKEN"])
    client = berserk.Client(session=session)
    try:
        my_id = client.account.get()['id']
    except:
        print("Lichess bağlantısı kurulamadı."); return

    bot = OxydanAegisV4(SETTINGS["ENGINE_PATH"], uci_options=config.get('engine', {}).get('uci_options', {}))
    active_games = set() 

    if config.get("matchmaking"):
        mm = Matchmaker(client, config, active_games) 
        threading.Thread(target=mm.start, daemon=True).start()

    print(f"🔥 Oxydan Aegis Hazır. ID: {my_id} | Max Slot: {SETTINGS['MAX_PARALLEL_GAMES']}", flush=True)

    while True:
        try:
            # Bağlantı koptuğunda API'yi spamlamamak için kısa bir bekleme
            time.sleep(0.5) 

            for event in client.bots.stream_incoming_events():
                cur_elapsed = time.time() - start_time
                should_stop = os.path.exists("STOP.txt") or cur_elapsed > SETTINGS["MAX_TOTAL_RUNTIME"]
                close_to_end = cur_elapsed > (SETTINGS["MAX_TOTAL_RUNTIME"] - (SETTINGS["STOP_ACCEPTING_MINS"] * 60))

                if event['type'] == 'challenge':
                    ch_id = event['challenge']['id']
                    if should_stop or close_to_end or len(active_games) >= SETTINGS["MAX_PARALLEL_GAMES"]:
                        client.challenges.decline(ch_id, reason='later')
                        if should_stop and len(active_games) == 0: sys.exit(0)
                    else:
                        client.challenges.accept(ch_id)

                elif event['type'] == 'gameStart':
                    game_id = event['game']['id']
                    if game_id not in active_games and len(active_games) < SETTINGS["MAX_PARALLEL_GAMES"]:
                        active_games.add(game_id)
                        threading.Thread(
                            target=handle_game_wrapper,
                            args=(client, game_id, bot, my_id, active_games),
                            daemon=True
                        ).start()

        except Exception as e:
            if "429" in str(e):
                print("🚨 Hız sınırı (429) aşıldı! 60 saniye bekleniyor..."); time.sleep(60)
            else:
                print(f"⚠️ Bağlantı hatası, yeniden deneniyor: {e}"); time.sleep(5)

if __name__ == "__main__":
    main()
