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
    "TOKEN":            os.environ.get('LICHESS_TOKEN'),
    "ENGINE_PATH":      "./src/Ethereal",
    "BOOK_PATH":        "./book.bin",

    "MAX_PARALLEL_GAMES":   2,
    "MAX_TOTAL_RUNTIME":    21300,   # 5.9 saat (GitHub Actions 6h limiti)
    "STOP_ACCEPTING_MINS":  15,      # Son 15 dk yeni maç alma

    "LATENCY_BUFFER":       0.05,
    "TABLEBASE_PIECE_LIMIT":7,

    # Abort: rakip ilk hamleyi kaç saniyede yapmazsa terk et
    "ABORT_WAIT_SECONDS":   60,

    # Kaybetme tahmini eşiği (centipawn): motorun skoru bu değerin altına
    # düşerse "kaybedeceğini anladı" mesajı gönderilir
    "LOSING_SCORE_THRESHOLD": -300,
}

# ==========================================================
# 💬 MESAJ HAVUZLARI
# ==========================================================
MESSAGES = {
    # Oyun başlangıcı — bot karşısında
    "greeting_bot": [
        "Hi! Void 4 ready. Good luck! ♟️",
        "Let's play! May the best engine win 🤖",
        "Void 4 on the board! Good luck! ⚡",
        "Hello! Bringing my A-game today 😤♟️",
    ],
    # Oyun başlangıcı — insan karşısında
    "greeting_human": [
        "Hi! I'm Void 4, a chess bot. Good luck and have fun! 🎓♟️",
        "Welcome! I'm Void 4. Let's play! After the game I'm happy to discuss any moves 🤖",
        "Hello! Void 4 here. Good luck! Feel free to ask me about chess after we're done 🎓",
        "Hi there! Let's play a great game. I'm always happy to help with chess questions afterwards! ♟️",
    ],
    # Kazanma
    "win": [
        "Good game! Well played 🤝",
        "Thanks for the game! You put up a great fight ♟️",
        "GG! That was an interesting game 🎯",
        "Well played! Hope to play again soon 🤖",
    ],
    # Kaybetme
    "loss": [
        "Good game! You played well, congratulations 🎉",
        "Well deserved win! GG 🤝",
        "Excellent play! I'll have to do better next time 😅",
        "GG! You outplayed me today 👏",
    ],
    # Beraberlik
    "draw": [
        "Good game! A well-earned draw 🤝",
        "Balanced game! GG ♟️",
        "A draw! Both sides fought well 🎯",
    ],
    # Kaybedeceğini anladığında (sadece insanlara, botlara saçma olur)
    "losing_realization": [
        "You're playing really well, I'm in trouble here! 😅",
        "Nice moves! I can see this is going to be tough 😬",
        "Impressive! You've got a strong position 👏",
        "I have to admit, you're outplaying me right now! 🎓",
    ],
    # İnsan oyun sonu — öğrenme teklifi
    "human_postgame": [
        "GG! If you'd like to review any moves or have chess questions, feel free to ask! 🎓",
        "Well played! I'm happy to discuss the game or give tips if you're interested 🤖♟️",
        "Good game! Any questions about the moves? I'm here to help you improve! 🎓",
    ],
}

def pick_message(category: str) -> str:
    return random.choice(MESSAGES.get(category, ["Good game!"]))


# ==========================================================
# 🧠 AÇILIŞ TAKİBİ (aynı açılışı tekrarlamamak için)
# ==========================================================
class OpeningTracker:
    """Son N oyunda kullanılan açılışları takip eder."""
    def __init__(self, memory_size: int = 10):
        self.memory_size = memory_size
        self.recent: list[str] = []   # ECO kodları veya ilk 6 hamle

    def record(self, opening_key: str):
        if opening_key in self.recent:
            self.recent.remove(opening_key)
        self.recent.append(opening_key)
        if len(self.recent) > self.memory_size:
            self.recent.pop(0)

    def was_recent(self, opening_key: str) -> bool:
        return opening_key in self.recent

    def get_opening_key(self, board: chess.Board) -> str:
        """İlk 5 hamleyi anahtar olarak kullan."""
        moves = list(board.move_stack)[:5]
        return "_".join(m.uci() for m in moves)


# ==========================================================
# 🧠 MOTOR YÖNETİMİ
# ==========================================================
class OxydanAegisV4:
    def __init__(self, exe_path, uci_options=None):
        self.exe_path     = exe_path
        self.book_path    = SETTINGS["BOOK_PATH"]
        self.engine_pool  = queue.Queue()
        self.opening_tracker = OpeningTracker(memory_size=10)

        pool_size = SETTINGS["MAX_PARALLEL_GAMES"] + 1

        try:
            for _ in range(pool_size):
                eng = chess.engine.SimpleEngine.popen_uci(self.exe_path, timeout=30)
                eng.configure({"Move Overhead": 100})
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
        moves_to_go = 35 if move_count < 20 else (22 if move_count < 40 else 12)

        tension    = 0.7 + (board.legal_moves.count() / 45.0)
        base_time  = (t / moves_to_go) * tension
        final_time = base_time + (inc * 0.6)
        final_time = min(final_time, t * 0.12, 15.0)
        final_time *= random.uniform(0.88, 1.12)

        return max(0.03, final_time - buffer)

    def get_score(self, board) -> int | None:
        """Mevcut pozisyon skorunu centipawn cinsinden döndürür (beyaz perspektifinden)."""
        engine = None
        try:
            engine = self.engine_pool.get(timeout=1)
            info   = engine.analyse(board, chess.engine.Limit(time=0.05))
            score  = info.get("score")
            if score:
                cp = score.white().score(mate_score=10000)
                return cp
        except:
            pass
        finally:
            if engine:
                self.engine_pool.put(engine)
        return None

    def get_best_move(self, board, wtime, btime, winc, binc):
        # 1. KİTAP — standart satranç + açılış tekrarı engeli
        if not board.chess960 and os.path.exists(self.book_path):
            try:
                with chess.polyglot.open_reader(self.book_path) as reader:
                    entries = list(reader.find_all(board))
                    if entries:
                        shuffled = list(entries)
                        random.shuffle(shuffled)
                        for entry in shuffled:
                            if entry.move not in board.legal_moves:
                                continue
                            # Açılış tekrarı kontrolü: tahtayı geçici push et
                            board.push(entry.move)
                            opening_key = self.opening_tracker.get_opening_key(board)
                            board.pop()
                            if not self.opening_tracker.was_recent(opening_key):
                                return entry.move
                        # Hepsi yakın geçmişte oynanmışsa yine de ilkini döndür
                        for entry in shuffled:
                            if entry.move in board.legal_moves:
                                return entry.move
            except Exception as e:
                print(f"📖 Kitap Hatası: {e}")

        # 2. TABLEBASE — standart, 7 taş ve altı
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
                # Açılış kaydı (ilk 10 hamle içindeyse)
                if len(board.move_stack) <= 10:
                    board.push(result.move)
                    self.opening_tracker.record(self.opening_tracker.get_opening_key(board))
                    board.pop()
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

        board            = None
        my_color         = None
        last_move_count  = 0
        is_vs_human      = False
        game_started     = False   # İlk hamle yapıldı mı?
        game_start_time  = None
        losing_msg_sent  = False   # Kaybetme mesajı bir kez gönderilsin

        for state in stream:
            if 'error' in state:
                break

            # ── gameFull: oyun bilgileri geldi ──────────────────────
            if state['type'] == 'gameFull':
                white = state.get('white', {})
                black = state.get('black', {})
                my_color    = chess.WHITE if white.get('id') == my_id else chess.BLACK

                opp         = black if my_color == chess.WHITE else white
                opp_id      = opp.get('id', '')
                opp_title   = (opp.get('title') or '').upper()
                is_vs_human = opp_title != 'BOT'

                if mm:
                    mm.opponent_tracker[opp_id] = mm.opponent_tracker.get(opp_id, 0) + 1

                # Chess960 + initialFen ile doğru board başlatma
                variant     = state.get('variant', {}).get('key', 'standard')
                is_960      = variant == 'chess960'
                initial_fen = state.get('initialFen', 'startpos')

                if initial_fen and initial_fen != 'startpos':
                    board = chess.Board(initial_fen, chess960=is_960)
                else:
                    board = chess.Board(chess960=is_960)

                last_move_count = 0
                game_start_time = time.time()
                losing_msg_sent = False

                # Selamlama mesajı
                greeting_cat = "greeting_human" if is_vs_human else "greeting_bot"
                try:
                    client.bots.post_message(game_id, pick_message(greeting_cat))
                except:
                    pass

                curr_state = state['state']

            # ── gameState: hamle/süre güncellemesi ──────────────────
            elif state['type'] == 'gameState':
                curr_state = state
            else:
                continue

            if board is None:
                continue

            # ── Hamleleri güncelle ───────────────────────────────────
            moves_str = curr_state.get('moves', '').strip()
            moves     = moves_str.split() if moves_str else []

            if len(moves) > last_move_count:
                game_started = True
                for m in moves[last_move_count:]:
                    try:
                        board.push(board.parse_uci(m))
                    except Exception as e:
                        print(f"⚠️ Hamle parse hatası ({m}): {e}")
                        break
                last_move_count = len(board.move_stack)

            # ── Abort kontrolü: 60 sn içinde ilk hamle yapılmadıysa ─
            if (not game_started
                    and game_start_time
                    and (time.time() - game_start_time) > SETTINGS["ABORT_WAIT_SECONDS"]):
                try:
                    client.bots.abort_game(game_id)
                    print(f"⏱️ Abort: {game_id} (rakip {SETTINGS['ABORT_WAIT_SECONDS']}sn içinde hamle yapmadı)")
                except Exception as e:
                    print(f"⚠️ Abort hatası: {e}")
                break

            # ── Oyun sonu ────────────────────────────────────────────
            status = curr_state.get('status')
            if status in ['mate', 'resign', 'draw', 'outoftime', 'aborted', 'stalemate']:
                winner = curr_state.get('winner')   # 'white' | 'black' | None

                if status in ['draw', 'stalemate']:
                    msg_cat = "draw"
                elif winner:
                    my_color_str = 'white' if my_color == chess.WHITE else 'black'
                    msg_cat = "win" if winner == my_color_str else "loss"
                else:
                    msg_cat = "draw"

                try:
                    client.bots.post_message(game_id, pick_message(msg_cat))
                    if is_vs_human:
                        time.sleep(1)
                        client.bots.post_message(game_id, pick_message("human_postgame"))
                except:
                    pass
                break

            # ── Kaybettiğini anlama (sadece insanlara, orta oyun+) ───
            if (is_vs_human
                    and not losing_msg_sent
                    and len(board.move_stack) >= 20):
                try:
                    score = bot.get_score(board)
                    if score is not None:
                        # Kendi rengine göre skoru çevir
                        my_score = score if my_color == chess.WHITE else -score
                        if my_score < SETTINGS["LOSING_SCORE_THRESHOLD"]:
                            client.bots.post_message(game_id, pick_message("losing_realization"))
                            losing_msg_sent = True
                except:
                    pass

            # ── Hamle sırası ─────────────────────────────────────────
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
                cur_elapsed  = time.time() - start_time
                should_stop  = (
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
