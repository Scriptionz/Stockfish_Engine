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
# ⚙️ AYARLAR
# ==========================================================
SETTINGS = {
    "TOKEN": os.environ.get('LICHESS_TOKEN'),
    "ENGINE_PATH": "./src/Ethereal",
    "BOOK_PATH": "./book.bin",

    "MAX_PARALLEL_GAMES": 2,
    "MAX_TOTAL_RUNTIME": 21300,       # 5.9 saat (GitHub Actions 6h limiti)
    "STOP_ACCEPTING_MINS": 15,        # Son 15 dk yeni maç alma

    "LATENCY_BUFFER": 0.05,
    "TABLEBASE_PIECE_LIMIT": 7,

    "GREETING":       "Oxydan 9 On The Board! Good luck! 🤖♟️",
    "GREETING_HUMAN": "Welcome! I'm Oxydan 9. Good luck! After the game feel free to ask me about any moves! 🎓♟️",
}

# ==========================================================
# 🧠 MOTOR YÖNETİMİ
# ==========================================================

class OxydanAegisV4:
    def __init__(self, exe_path, uci_options=None):
        self.exe_path = exe_path
        self.book_path = SETTINGS["BOOK_PATH"]
        self.engine_pool = queue.Queue()

        pool_size = SETTINGS["MAX_PARALLEL_GAMES"] + 1

        try:
            for _ in range(pool_size):
                eng = chess.engine.SimpleEngine.popen_uci(self.exe_path, timeout=30)
                eng.configure({"Move Overhead": 100})  # 500ms bullet'ı öldürür, 100ms yeterli
                if uci_options:
                    for opt, val in uci_options.items():
                        try: eng.configure({opt: val})
                        except: pass
                self.engine_pool.put(eng)
            print(f"🚀 {pool_size} Motor Hazır.", flush=True)
        except Exception as e:
            print(f"KRİTİK HATA: {e}", flush=True)
            sys.exit(1)

    def to_seconds(self, t):
        if t is None: return 0.0
        if isinstance(t, timedelta): return t.total_seconds()
        try:
            val = float(t)
            return val / 1000.0 if val > 1000 else val
        except:
            return 0.0

    def calculate_smart_time(self, t, inc, board):
        buffer = SETTINGS.get("LATENCY_BUFFER", 0.05)

        if t < 0.4:
            return 0.01
        if t < 1.0:
            return max(0.02, (t * 0.12) + (inc * 0.9) - buffer)

        move_count = len(board.move_stack)
        if move_count < 20:
            moves_to_go = 35
        elif move_count < 40:
            moves_to_go = 22
        else:
            moves_to_go = 12

        legal_moves = board.legal_moves.count()
        tension = 0.7 + (legal_moves / 45.0)

        base_time  = (t / moves_to_go) * tension
        final_time = base_time + (inc * 0.6)

        max_allowed = t * 0.12
        final_time  = min(final_time, max_allowed, 15.0)

        # İnsansı varyasyon ±12%
        final_time *= random.uniform(0.88, 1.12)

        return max(0.03, final_time - buffer)

    def get_best_move(self, board, wtime, btime, winc, binc):
        # 1. KİTAP — sadece standart satranç
        if not board.chess960 and os.path.exists(self.book_path):
            try:
                with chess.polyglot.open_reader(self.book_path) as reader:
                    entries = list(reader.find_all(board))
                    if entries:
                        shuffled = list(entries)
                        random.shuffle(shuffled)
                        for entry in shuffled:
                            if entry.move in board.legal_moves:
                                return entry.move
            except Exception as e:
                print(f"📖 Kitap Hatası: {e}")

        # 2. TABLEBASE — sadece standart, 7 taş ve altı
        if not board.chess960 and len(board.piece_map()) <= SETTINGS["TABLEBASE_PIECE_LIMIT"]:
            try:
                r = requests.get(
                    f"https://tablebase.lichess.ovh/standard?fen={board.fen()}",
                    timeout=0.5
                )
                if r.status_code == 200:
                    data = r.json()
                    if data.get("moves"):
                        best = chess.Move.from_uci(data["moves"][0]["uci"])
                        if best in board.legal_moves:
                            return best
            except:
                pass

        # 3. MOTOR (Ethereal)
        engine = None
        try:
            engine = self.engine_pool.get()
            my_time = self.to_seconds(wtime if board.turn == chess.WHITE else btime)
            my_inc  = self.to_seconds(winc  if board.turn == chess.WHITE else binc)
            think   = self.calculate_smart_time(my_time, my_inc, board)

            result = engine.play(board, chess.engine.Limit(time=think))

            if result.move and result.move in board.legal_moves:
                return result.move
            print(f"⚠️ Motor yasal olmayan hamle: {result.move}, fallback.")
        except Exception as e:
            print(f"🚨 Motor Hatası: {e}")
        finally:
            if engine:
                self.engine_pool.put(engine)

        legal = list(board.legal_moves)
        return legal[0] if legal else None


# ==========================================================
# 🎮 OYUN YÖNETİMİ
# ==========================================================

def handle_game(client, game_id, bot, my_id, mm):
    try:
        stream = client.bots.stream_game_state(game_id)

        board          = None
        my_color       = None
        last_move_count = 0
        is_vs_human    = False

        for state in stream:
            if 'error' in state:
                break

            if state['type'] == 'gameFull':
                white = state.get('white', {})
                black = state.get('black', {})
                my_color = chess.WHITE if white.get('id') == my_id else chess.BLACK

                opp       = black if my_color == chess.WHITE else white
                opp_id    = opp.get('id', '')
                opp_title = (opp.get('title') or '').upper()
                is_vs_human = opp_title != 'BOT'

                # Rakip takibi (rematch limiti için)
                if mm:
                    mm.opponent_tracker[opp_id] = mm.opponent_tracker.get(opp_id, 0) + 1

                # ✅ Chess960 + initialFen ile doğru board başlatma (abort düzeltmesi)
                variant     = state.get('variant', {}).get('key', 'standard')
                is_960      = variant == 'chess960'
                initial_fen = state.get('initialFen', 'startpos')

                if initial_fen and initial_fen != 'startpos':
                    board = chess.Board(initial_fen, chess960=is_960)
                else:
                    board = chess.Board(chess960=is_960)

                last_move_count = 0

                # Selamlama
                greeting = SETTINGS["GREETING_HUMAN"] if is_vs_human else SETTINGS["GREETING"]
                try:
                    client.bots.post_message(game_id, greeting)
                except:
                    pass

                curr_state = state['state']

            elif state['type'] == 'gameState':
                curr_state = state
            else:
                continue

            if board is None:
                continue

            # ✅ Hamleleri güncelle (Chess960 rok düzeltmesi: parse_uci + push)
            moves_str = curr_state.get('moves', '').strip()
            moves     = moves_str.split() if moves_str else []

            if len(moves) > last_move_count:
                for m in moves[last_move_count:]:
                    try:
                        board.push(board.parse_uci(m))
                    except Exception as e:
                        print(f"⚠️ Hamle parse hatası ({m}): {e}")
                        break
                last_move_count = len(board.move_stack)

            status = curr_state.get('status')
            if status in ['mate', 'resign', 'draw', 'outoftime', 'aborted', 'stalemate']:
                if is_vs_human:
                    try:
                        client.bots.post_message(
                            game_id,
                            "Good game! 🎓 If you have questions about any moves or want chess tips, feel free to ask!"
                        )
                    except:
                        pass
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
    try:
        handle_game(client, game_id, bot, my_id, mm)
    finally:
        active_games.discard(game_id)


# ==========================================================
# 🚀 ANA DÖNGÜ
# ==========================================================

def main():
    start_time = time.time()
    session    = berserk.TokenSession(SETTINGS["TOKEN"])
    client     = berserk.Client(session=session)

    try:
        with open("config.yml", "r") as f:
            config = yaml.safe_load(f)
        my_id = client.account.get()['id']
    except Exception as e:
        print(f"Bağlantı Hatası: {e}")
        return

    bot = OxydanAegisV4(
        SETTINGS["ENGINE_PATH"],
        uci_options=config.get('engine', {}).get('uci_options', {})
    )
    active_games = set()

    mm = None
    if config.get("matchmaking"):
        mm = Matchmaker(client, config, active_games, token=SETTINGS["TOKEN"])
        threading.Thread(target=mm.start, daemon=True).start()

    print(f"🔥 Oxydan 9 Hazır. ID: {my_id}", flush=True)

    while True:
        try:
            for event in client.bots.stream_incoming_events():
                cur_elapsed = time.time() - start_time
                should_stop = (
                    os.path.exists("STOP.txt") or
                    cur_elapsed > SETTINGS["MAX_TOTAL_RUNTIME"]
                )
                close_to_end = cur_elapsed > (
                    SETTINGS["MAX_TOTAL_RUNTIME"] -
                    (SETTINGS["STOP_ACCEPTING_MINS"] * 60)
                )

                if event['type'] == 'challenge':
                    ch    = event['challenge']
                    ch_id = ch['id']

                    accept, reason = True, 'policy'
                    if mm:
                        accept, reason = mm.is_challenge_acceptable(ch)

                    can_accept = (
                        not should_stop and
                        not close_to_end and
                        len(active_games) < SETTINGS["MAX_PARALLEL_GAMES"] and
                        accept
                    )

                    if can_accept:
                        client.challenges.accept(ch_id)
                        print(f"✅ Kabul: {ch_id} — {reason}", flush=True)
                    else:
                        decline_reason = 'later' if (should_stop or close_to_end) else 'generic'
                        client.challenges.decline(ch_id, reason=decline_reason)
                        print(f"❌ Reddedildi: {ch_id} — {reason}", flush=True)
                        if should_stop and len(active_games) == 0:
                            os._exit(0)

                elif event['type'] == 'gameStart':
                    game_id = event['game']['id']
                    if game_id not in active_games:
                        active_games.add(game_id)
                        threading.Thread(
                            target=handle_game_wrapper,
                            args=(client, game_id, bot, my_id, active_games, mm),
                            daemon=True
                        ).start()

        except Exception as e:
            print(f"⚠️ Akış koptu: {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
