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
    "LATENCY_BUFFER": 0.2,       # Saniye cinsinden ağ gecikme payı (150ms)
    "TABLEBASE_PIECE_LIMIT": 6,   # Kaç taş kalınca tablebase'e sorsun? (6 güvenlidir)
    "MIN_THINK_TIME": 0.05,       # En az düşünme süresi
    
    # --- MESAJLAR ---
    "GREETING": "Void v1 Active. System stabilized.",
}
# ==========================================================

class OxydanAegisV4:
    def __init__(self, exe_path, uci_options=None):
        self.exe_path = exe_path
        self.book_path = SETTINGS["BOOK_PATH"]
        self.uci_options = uci_options
        self.engine_pool = queue.Queue()
        
        # Havuz Boyutu: Paralel maç sayısı + 1 (Yedek ünite)
        pool_size = SETTINGS["MAX_PARALLEL_GAMES"] + 1
        
        try:
            for i in range(pool_size):
                eng = chess.engine.SimpleEngine.popen_uci(self.exe_path, timeout=30)
                if uci_options:
                    for opt, val in uci_options.items():
                        try: eng.configure({opt: val})
                        except: pass
                self.engine_pool.put(eng)
            print(f"🚀 Oxydan v7: {pool_size} Motor Ünitesi Havuza Alındı.", flush=True)
        except Exception as e:
            print(f"KRİTİK HATA: Motorlar başlatılamadı: {e}", flush=True)
            sys.exit(1)

    def to_seconds(self, t):
        if t is None: return 0.0
        if isinstance(t, timedelta): return t.total_seconds()
        try:
            val = float(t)
            return val / 1000.0 if val > 1000 else val
        except: return 0.0

    def calculate_smart_time(self, t, inc, board):
        move_num = board.fullmove_number if board else 1
        
        # 1. ACİL DURUM (3 saniye altı panik modu)
        if t < 3.0:
            return 0.05 if t > 1.0 else 0.02

        # 2. TEMPO ANALİZİ (MTG - Moves To Go)
        if t > 600: mtg = 45   # Classical
        elif t > 180: mtg = 35 # Rapid
        else: mtg = 25         # Blitz
        
        if move_num > 60: mtg = max(15, mtg - 10)

        # 3. BÜTÇE VE KARMAŞIKLIK
        base_budget = (t / mtg) + (inc * 0.85)
        legal_moves = board.legal_moves.count()
        complexity = 1.3 if legal_moves > 40 else (0.7 if legal_moves < 15 else 1.0)
        target_time = base_budget * complexity

        # 4. GÜVENLİK SINIRLARI
        if t < 10.0:
            target_time = min(target_time, t / 45)
            min_think = SETTINGS["MIN_THINK_TIME"]
        else:
            min_think = 0.3 if t > 30 else 0.1

        max_limit = t * 0.15 # Tek hamlede bütçenin %15'inden fazlasını harcama
        final_time = max(min_think, min(target_time, max_limit))
        
        return max(0.01, final_time - SETTINGS["LATENCY_BUFFER"])

    def get_best_move(self, board, wtime, btime, winc, binc):
        """
        Oxydan Bot Hamle Karar Mekanizması:
        1. Cerebellum Book (.bin) -> Açılış
        2. Syzygy API -> Oyun Sonu (<= 6 taş)
        3. Ethereal Engine -> Orta Oyun
        """
        
        if os.path.exists(SETTINGS["BOOK_PATH"]):
            try:
                with chess.polyglot.open_reader(SETTINGS["BOOK_PATH"]) as reader:
                    best_entry = None
                    # Tahtadaki konum için tüm hamleleri tara, en yüksek ağırlıklıyı seç
                    for entry in reader.find_all(board):
                        if best_entry is None or entry.weight > best_entry.weight:
                            best_entry = entry
                    
                    if best_entry:
                        print(f"📖 Cerebellum Kitap Hamlesi: {best_entry.move} (W: {best_entry.weight})", flush=True)
                        return best_entry.move
            except Exception as e:
                print(f"⚠️ Kitap okunurken hata: {e}", flush=True)

        # --- 2. ADIM: AKILLI SYZYGY TABLEBASE (Oyun Sonu) ---
        try:
            # Sıradaki oyuncunun kalan süresini al (milisaniyeden saniyeye çevir)
            current_time_ms = wtime if board.turn == chess.WHITE else btime
            current_time_sec = self.to_seconds(current_time_ms)
            
            # Strateji: 30 saniyeden fazla süre varsa 7 taş, azsa 6 taş sorgula
            syzygy_limit = 7 if current_time_sec > 30 else 6
            
            if len(board.piece_map()) <= syzygy_limit:
                fen = board.fen().replace(" ", "_")
                # Süre azaldıkça API'yi bekleme süresini (timeout) de kısaltıyoruz
                api_timeout = 0.5 if current_time_sec > 10 else 0.3
                
                r = requests.get(f"https://tablebase.lichess.ovh/standard?fen={fen}", timeout=api_timeout)
                
                if r.status_code == 200:
                    data = r.json()
                    if "moves" in data and len(data["moves"]) > 0:
                        tb_move_uci = data["moves"][0]["uci"]
                        print(f"🧩 Syzygy ({syzygy_limit}-Piece) Hamlesi: {tb_move_uci}", flush=True)
                        return chess.Move.from_uci(tb_move_uci)
        except Exception as e:
            # API yavaşsa veya hata verirse vakit kaybetmeden motora pasla
            print(f"⚠️ Syzygy atlandı (Hata veya Zaman Aşımı): {e}", flush=True)

        # --- 3. ADIM: MOTOR HESAPLAMA (Ethereal) ---
        # Eğer kitapta hamle yoksa veya oyun sonuna girilmemişse motor devreye girer
        engine = self.engine_pool.get()
        try:
            # Sıra kimdeyse onun süresini ve artışını (inc) al
            my_time = wtime if board.turn == chess.WHITE else btime
            my_inc = winc if board.turn == chess.WHITE else binc
            
            # Daha önce tanımladığın akıllı zaman yönetimi fonksiyonu (calculate_smart_time)
            # Eğer o fonksiyonun adını değiştirdiysen burayı da güncelle.
            think_time = self.calculate_smart_time(self.to_seconds(my_time), self.to_seconds(my_inc), board)
            
            # Motoru belirtilen süre sınırıyla çalıştır
            result = engine.play(board, chess.engine.Limit(time=think_time))
            
            print(f"⚙️ Motor Hamlesi: {result.move} (Süre: {think_time:.2f}s)", flush=True)
            return result.move
            
        except Exception as e:
            print(f"🚨 Motor hatası: {e}", flush=True)
            # Motor hata verirse bile botun çökmemesi için rastgele bir hamle döndür (acil durum)
            return list(board.legal_moves)[0]
        finally:
            # Motoru havuza geri bırak
            self.engine_pool.put(engine)

        # 2. TABLEBASE (7 taş ve altı için online sorgu)
        if len(board.piece_map()) <= SETTINGS.get("TABLEBASE_PIECE_LIMIT", 6):
            try:
                fen_tb = board.fen().replace(" ", "_")
                r_tb = requests.get(f"https://tablebase.lichess.ovh/standard?fen={fen_tb}", timeout=0.5)
                if r_tb.status_code == 200:
                    data_tb = r_tb.json()
                    if data_tb.get("moves"):
                        print(f"🎯 Tablebase Hamlesi: {data_tb['moves'][0]['uci']}", flush=True)
                        return chess.Move.from_uci(data_tb["moves"][0]["uci"])
            except:
                pass

        # 3. MOTOR HESAPLAMA (Kitap bittiğinde veya API yanıt vermediğinde)
        engine = self.engine_pool.get()
        try:
            my_time = wtime if board.turn == chess.WHITE else btime
            my_inc = winc if board.turn == chess.WHITE else binc
            
            t_sec = self.to_seconds(my_time)
            i_sec = self.to_seconds(my_inc)
            
            think_time = self.calculate_smart_time(t_sec, i_sec, board)
            
            # Motor hesaplama limiti
            result = engine.play(board, chess.engine.Limit(time=think_time))
            return result.move
            
        except Exception as e:
            print(f"❌ Motor Hatası: {e}")
            # Acil durum hamlesi (legal hamlelerden ilkini yap)
            return next(iter(board.legal_moves)) if board.legal_moves else None
        finally:
            # Motoru her durumda havuza geri ver
            self.engine_pool.put(engine)

def handle_game(client, game_id, bot, my_id):
    try:
        client.bots.post_message(game_id, SETTINGS["GREETING"])
        stream = client.bots.stream_game_state(game_id)
        my_color = None

        for state in stream:
            if 'error' in state: break

            if state['type'] == 'gameFull':
                my_color = chess.WHITE if state['white'].get('id') == my_id else chess.BLACK
                curr_state = state['state']
            elif state['type'] == 'gameState':
                curr_state = state
            else: continue

            moves = curr_state.get('moves', "").split()
            board = chess.Board()
            for m in moves: board.push_uci(m)

            if curr_state.get('status') in ['mate', 'resign', 'draw', 'outoftime', 'aborted', 'stalemate']:
                break

            if board.turn == my_color and not board.is_game_over():
                wtime, btime = curr_state.get('wtime'), curr_state.get('btime')
                winc, binc = curr_state.get('winc'), curr_state.get('binc')
                move = bot.get_best_move(board, wtime, btime, winc, binc)
                
                if move:
                    for attempt in range(3):
                        try:
                            client.bots.make_move(game_id, move.uci())
                            break 
                        except:
                            time.sleep((attempt + 1) * 1)
    except Exception as e:
        print(f"Oyun Hatası ({game_id}): {e}", flush=True)

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
        print("HATA: config.yml okunamadı.")
        return

    session = berserk.TokenSession(SETTINGS["TOKEN"])
    client = berserk.Client(session=session)
    try:
        my_id = client.account.get()['id']
    except:
        print("Lichess bağlantısı kurulamadı.")
        return

    bot = OxydanAegisV4(SETTINGS["ENGINE_PATH"], uci_options=config.get('engine', {}).get('uci_options', {}))
    active_games = set() 

    if config.get("matchmaking"):
        mm = Matchmaker(client, config, active_games) 
        threading.Thread(target=mm.start, daemon=True).start()

    print(f"🔥 Oxydan Aegis Hazır. ID: {my_id} | Max Slot: {SETTINGS['MAX_PARALLEL_GAMES']}", flush=True)

    while True:
        try:
            stop_signal = os.path.exists("STOP.txt")
            elapsed = time.time() - start_time

            # Kritik zaman kontrolü
            if elapsed > SETTINGS["MAX_TOTAL_RUNTIME"]:
                print("🛑 Toplam süre doldu. Kapanıyor.")
                sys.exit(0)

            for event in client.bots.stream_incoming_events():
                # Stream içindeyken periyodik kontroller
                cur_elapsed = time.time() - start_time
                should_stop = os.path.exists("STOP.txt") or cur_elapsed > SETTINGS["MAX_TOTAL_RUNTIME"]
                
                # Yeni maç kabul etmeme sınırı (son 15 dk)
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
                print("🚨 Hız sınırı (429). Bekleniyor...")
                time.sleep(60)
            else:
                time.sleep(5)

if __name__ == "__main__":
    main()
