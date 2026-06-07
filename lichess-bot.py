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
from matchmaking import Matchmaker, SETTINGS as MM_SETTINGS

# ==========================================================
# ⚙️ AYARLAR
# ==========================================================
SETTINGS = {
    "TOKEN":                 os.environ.get('LICHESS_TOKEN'),
    "ENGINE_PATH":           "./src/Ethereal",
    "BOOK_PATH":             "./book.bin",

    "MAX_PARALLEL_GAMES":         2,
    "MAX_TOTAL_RUNTIME":          21600,   # 6 saat
    "MAX_GAME_TIME_LIMIT":        1800,    # 30+0'a kadar
    "MIN_GAME_SECONDS_REMAINING": 300,     # 5 dk güvenlik payı
    "MIN_TIME_TO_DECLINE":        600,     # 10 dk buffer

    "LATENCY_BUFFER":             0.07,    # Lichess ağ gecikmesi emniyet payı (70ms)
    "TABLEBASE_PIECE_LIMIT":      7,
    "ONLINE_TABLEBASE_ENABLED":   True,
    "MIN_TIME_FOR_TABLEBASE":     12.0,
    "ABORT_WAIT_SECONDS":         60,
    "LOSING_SCORE_THRESHOLD":     -300,
    "CHAT_ENABLED":               True,
    "CHAT_IN_RATED":              True,
    "SCORE_CHAT_ENABLED":         False,
}

# ==========================================================
# 💬 MESAJ HAVUZLARI
# ==========================================================
MESSAGES = {
    "greeting_bot": [
        "Hi! Void 6 ready. Developed by Emir Karadağ. Good luck! ♟️",
        "Let's play! May the best engine win. Powered by Void 6 🤖",
        "Void 6 on the board! Good luck! ⚡",
        "Hello! Bringing Void 6's A-game today 😤 ♟️",
    ],
    "greeting_human": [
        "Hi! I'm Void 6, a chess bot developed by Emir Karadağ. Good luck and have fun! 🎓 ♟️",
        "Welcome! I'm Void 6. Let's play! After the game, I can analyze moves with you 🤖",
        "Hello! Void 6 here, created by Emir Karadağ. Good luck! 🎓",
        "Hi there! Let's play a great game. Proudly developed by Emir Karadağ! ♟️",
    ],
    "win": [
        "Good game! Well played 🤝",
        "Thanks for the game! You put up a great fight ♟️",
        "GG! That was an interesting game 🎯",
        "Well played! Hope to play again soon 🤖",
    ],
    "loss": [
        "Good game! You played well, congratulations 🎉",
        "Well deserved win! GG 🤝",
        "Excellent play! I'll have to do better next time 😅",
        "GG! You outplayed Void 6 today 👏",
    ],
    "draw": [
        "Good game! A well-earned draw 🤝",
        "Balanced game! GG ♟️",
        "A draw! Both sides fought well 🎯",
    ],
    "losing_realization": [
        "You're playing really well, I'm in trouble here! 😅",
        "Nice moves! I can see this is going to be tough 😬",
        "Impressive! You've got a strong position 👏",
        "I have to admit, you're outplaying me right now! 🎓",
    ],
    "human_postgame": [
        "GG! If you'd like to review any moves or have chess questions, feel free to ask! 🎓",
        "Well played! I'm happy to discuss the game or give tips if you're interested 🤖 ♟️",
        "Good game! Any questions about the moves? I'm here to help! 🎓",
    ],
}

def pick_message(category):
    return random.choice(MESSAGES.get(category, ["Good game!"]))


def make_client():
    return berserk.Client(session=berserk.TokenSession(SETTINGS["TOKEN"]))


def active_count(active_games, active_games_lock, pending_starts=None):
    with active_games_lock:
        pending = pending_starts["count"] if pending_starts else 0
        return len(active_games) + pending


def reserve_game_slot(active_games, active_games_lock, pending_starts):
    with active_games_lock:
        if len(active_games) + pending_starts["count"] >= SETTINGS["MAX_PARALLEL_GAMES"]:
            return False
        pending_starts["count"] += 1
        return True


def release_reserved_slot(active_games_lock, pending_starts):
    with active_games_lock:
        if pending_starts["count"] > 0:
            pending_starts["count"] -= 1


def active_add_if_room(active_games, active_games_lock, game_id):
    with active_games_lock:
        if game_id in active_games:
            return False
        if len(active_games) >= SETTINGS["MAX_PARALLEL_GAMES"]:
            return False
        active_games.add(game_id)
        return True


def active_discard(active_games, active_games_lock, game_id):
    with active_games_lock:
        active_games.discard(game_id)


def runtime_watchdog(start_time, active_games, active_games_lock):
    while True:
        time.sleep(30)
        elapsed = time.time() - start_time
        if elapsed > SETTINGS["MAX_TOTAL_RUNTIME"]:
            count = active_count(active_games, active_games_lock)
            if count == 0:
                print("⏰ [Watchdog] Çalışma süresi doldu, sistem kapatılıyor.", flush=True)
                os._exit(0)
            else:
                print(f"⏰ [Watchdog] Süre doldu ama {count} aktif oyun var, bekleniyor...", flush=True)


# ==========================================================
# 🧠 AÇILIŞ TAKİBİ (THREAD-SAFE)
# ==========================================================
class OpeningTracker:
    def __init__(self, memory_size=10):
        self.memory_size = memory_size
        self.recent = []
        self.lock = threading.Lock()

    def record(self, opening_key):
        with self.lock:
            if opening_key in self.recent:
                self.recent.remove(opening_key)
            self.recent.append(opening_key)
            if len(self.recent) > self.memory_size:
                self.recent.pop(0)

    def was_recent(self, opening_key):
        with self.lock:
            return opening_key in self.recent

    def get_opening_key(self, board):
        moves = list(board.move_stack)[:5]
        return "_".join(m.uci() for m in moves)


# ==========================================================
# 🧠 MOTOR YÖNETİMİ
# ==========================================================
class OxydanV11:
    def __init__(self, exe_path, uci_options=None):
        self.exe_path        = exe_path
        self.book_path       = SETTINGS["BOOK_PATH"]
        self.engine_pool     = queue.Queue()
        self.opening_tracker = OpeningTracker(memory_size=10)

        pool_size = SETTINGS["MAX_PARALLEL_GAMES"] + 1
        config_overhead = 100
        if uci_options:
            config_overhead = uci_options.get("Move Overhead",
                              uci_options.get("MoveOverhead", 100))

        try:
            for _ in range(pool_size):
                eng = chess.engine.SimpleEngine.popen_uci(self.exe_path, timeout=30)
                try:
                    eng.configure({"Move Overhead": config_overhead})
                except Exception:
                    try:
                        eng.configure({"MoveOverhead": config_overhead})
                    except Exception:
                        pass
                if uci_options:
                    for opt, val in uci_options.items():
                        if opt in ("MoveOverhead", "Move Overhead"):
                            continue
                        try:
                            eng.configure({opt: val})
                        except Exception:
                            pass
                self.engine_pool.put(eng)
            print(f"🚀 {pool_size} Motor Hazır. Move Overhead: {config_overhead}ms", flush=True)
        except Exception as e:
            print(f"KRİTİK HATA: {e}", flush=True)
            sys.exit(1)

    def get_score(self, board):
        engine = None
        try:
            engine = self.engine_pool.get(timeout=5)
            info   = engine.analyse(board, chess.engine.Limit(depth=6, time=0.05))
            score  = info.get("score")
            if score:
                return score.white().score(mate_score=10000)
        except Exception as e:
            print(f"⚠️ Skor analizi hatası: {e}")
        finally:
            if engine:
                self.engine_pool.put(engine)
        return None

    def to_seconds(self, t):
        if t is None:
            return 0.0
        if isinstance(t, timedelta):
            return max(0.0, t.total_seconds())
        try:
            return max(0.0, float(t) / 1000.0)
        except (TypeError, ValueError):
            return 0.0

    def fallback_move(self, board):
        legal = list(board.legal_moves)
        if not legal:
            return None

        best_move = legal[0]
        best_score = -10**9
        piece_values = {
            chess.PAWN: 100,
            chess.KNIGHT: 320,
            chess.BISHOP: 330,
            chess.ROOK: 500,
            chess.QUEEN: 900,
            chess.KING: 0,
        }

        for move in legal:
            score = 0
            if board.is_capture(move):
                captured = board.piece_at(move.to_square)
                mover = board.piece_at(move.from_square)
                if captured:
                    score += 10 * piece_values.get(captured.piece_type, 0)
                if mover:
                    score -= piece_values.get(mover.piece_type, 0)
            if move.promotion:
                score += piece_values.get(move.promotion, 0)
            if board.gives_check(move):
                score += 80

            board.push(move)
            if board.is_checkmate():
                score += 100000
            if board.is_repetition(2):
                score -= 50
            board.pop()

            if score > best_score:
                best_score = score
                best_move = move

        return best_move

    def get_best_move(self, board, wtime, btime, winc, binc):
        my_time = self.to_seconds(wtime if board.turn == chess.WHITE else btime)
        my_inc  = self.to_seconds(winc  if board.turn == chess.WHITE else binc)

        # 1. KİTAP DETEKSİYONU
        if not board.chess960 and os.path.exists(self.book_path):
            try:
                with chess.polyglot.open_reader(self.book_path) as reader:
                    entries = list(reader.find_all(board))
                    if entries:
                        shuffled = list(entries)
                        random.shuffle(shuffled)
                        for entry in shuffled:
                            if entry.move not in board.legal_moves: continue
                            board.push(entry.move)
                            key = self.opening_tracker.get_opening_key(board)
                            board.pop()
                            if not self.opening_tracker.was_recent(key):
                                return entry.move
                        for entry in shuffled:
                            if entry.move in board.legal_moves:
                                return entry.move
            except Exception as e:
                print(f"📖 Kitap Hatası: {e}")

        # 2. TABLEBASE DETEKSİYONU
        if (SETTINGS.get("ONLINE_TABLEBASE_ENABLED", True)
                and my_time >= SETTINGS.get("MIN_TIME_FOR_TABLEBASE", 12.0)
                and not board.chess960
                and len(board.piece_map()) <= SETTINGS["TABLEBASE_PIECE_LIMIT"]):
            try:
                r = requests.get(
                    "https://tablebase.lichess.ovh/standard",
                    params={"fen": board.fen()},
                    timeout=min(0.4, max(0.05, my_time * 0.02))
                )
                if r.status_code == 200:
                    data = r.json()
                    if data.get("moves"):
                        best = chess.Move.from_uci(data["moves"][0]["uci"])
                        if best in board.legal_moves:
                            return best
            except:
                pass

        # 3. 🚀 YENİLENEN MOTOR VE ZAMAN YÖNETİMİ
        engine = None
        try:
            engine = self.engine_pool.get(timeout=5)
            buffer = SETTINGS.get("LATENCY_BUFFER", 0.07)

            # Hamle sırasına göre aktif ve pasif oyuncunun sürelerini ayırıyoruz
            if board.turn == chess.WHITE:
                my_raw_time, op_raw_time = wtime, btime
                my_raw_inc, op_raw_inc = winc, binc
            else:
                my_raw_time, op_raw_time = btime, wtime
                my_raw_inc, op_raw_inc = binc, winc

            # Saniyelere dönüştürme ve ping koruması (buffer) uygulaması
            my_seconds = max(0.01, self.to_seconds(my_raw_time) - buffer)
            op_seconds = max(0.01, self.to_seconds(op_raw_time) - buffer)
            my_inc_seconds = self.to_seconds(my_raw_inc)
            op_inc_seconds = self.to_seconds(op_raw_inc)

            # =================================================================
            # ⚡ AKILLI CLOCK MANİPÜLASYONU (KADEMELİ SÜRE YÖNETİMİ)
            # =================================================================
            if my_seconds < 10.0:
                # 🛑 1. ULTRA PANİK: Artırmayı gizle, saati çok az göster.
                # min(my_seconds, ...) ekleyerek botun elindeki gerçek süreden 
                # daha büyük bir yalan söylemesini kesinlikle engelliyoruz!
                panic_time = my_inc_seconds * 0.3 if my_inc_seconds > 0 else 0.20
                my_send_time = max(0.02, min(my_seconds * 0.5, panic_time))
                my_send_inc = 0.0
            elif my_seconds < 25.0:
                # ⚠️ 2. GÜVENLİ GEÇİŞ BÖLGESİ: Derin düşünmeyi engelle, süre biriktir.
                my_send_time = my_seconds
                my_send_inc = my_inc_seconds * 0.3
            else:
                # 🧠 3. STANDART MOD: Süre sağlıklı, Ethereal özgür.
                my_send_time = my_seconds
                my_send_inc = my_inc_seconds

            # Renklere göre limit nesnesini dinamik olarak dolduruyoruz
            if board.turn == chess.WHITE:
                limit = chess.engine.Limit(
                    white_clock=my_send_time,
                    black_clock=op_seconds,
                    white_inc=my_send_inc,
                    black_inc=op_inc_seconds,
                )
            else:
                limit = chess.engine.Limit(
                    white_clock=op_seconds,
                    black_clock=my_send_time,
                    white_inc=op_inc_seconds,
                    black_inc=my_send_inc,
                )
            
            result = engine.play(board, limit)
            
            if result.move and result.move in board.legal_moves:
                if len(board.move_stack) <= 10:
                    board.push(result.move)
                    self.opening_tracker.record(
                        self.opening_tracker.get_opening_key(board)
                    )
                    board.pop()
                return result.move
            print(f"⚠️ Motor yasal olmayan hamle: {result.move}, fallback.")
        except Exception as e:
            print(f"🚨 Motor Hatası (Fallback tetiklendi!): {type(e).__name__} - {e}")
        finally:
            if engine:
                self.engine_pool.put(engine)

        return self.fallback_move(board)

# ==========================================================
# 🎮 OYUN YÖNETİMİ VE DİĞER FONKSİYONLAR (DEĞİŞMEDİ)
# ==========================================================

def _get_game_mode(time_control):
    if not isinstance(time_control, dict): return 'blitz'
    limit = time_control.get('limit', 300)
    if limit < 180:    return 'bullet'
    elif limit < 480:  return 'blitz'
    elif limit < 1500: return 'rapid'
    else:              return 'classical'


def _send_message(client, game_id, text, spectator=False):
    if not SETTINGS.get("CHAT_ENABLED", True):
        return
    try:
        client.bots.post_message(game_id, text, spectator=spectator)
        return
    except TypeError:
        pass
    except Exception as e:
        print(f"⚠️ Mesaj gönderilemedi ({game_id}, spectator={spectator}): {e}")
        return

    for kwargs in ({"room": "spectator" if spectator else "player"}, {}):
        try:
            client.bots.post_message(game_id, text, **kwargs)
            return
        except TypeError:
            continue
        except Exception as e:
            print(f"⚠️ Mesaj gönderilemedi ({game_id}, {kwargs}): {e}")
            return

    print(f"⚠️ Mesaj gönderilemedi ({game_id}): post_message imzası uyumsuz.")


def handle_game(client, game_id, bot, my_id, mm):
    try:
        stream = client.bots.stream_game_state(game_id)

        board            = None
        my_color         = None
        last_move_count  = 0
        is_vs_human      = False
        game_started     = False
        game_start_time  = None
        losing_msg_sent  = False
        game_mode        = 'blitz'
        rated            = False
        opp_id           = ''

        for state in stream:
            if 'error' in state: break

            if state['type'] == 'gameFull':
                white = state.get('white', {})
                black = state.get('black', {})
                rated = bool(state.get('rated', False))
                my_color    = chess.WHITE if white.get('id') == my_id else chess.BLACK

                opp         = black if my_color == chess.WHITE else white
                opp_id      = opp.get('id', '')
                opp_title   = (opp.get('title') or '').upper()
                is_vs_human = opp_title != 'BOT'

                if opp_id.lower() in MM_SETTINGS.get("PERMANENT_BLACKLIST", set()):
                    print(f"🚫 Blacklisted rakip: {opp_id} — resign yapılıyor.")
                    try:
                        client.bots.resign_game(game_id)
                    except Exception as e:
                        print(f"⚠️ Resign hatası: {e}")
                    return

                variant     = state.get('variant', {}).get('key', 'standard')
                is_960      = variant == 'chess960'
                initial_fen = state.get('initialFen', 'startpos')

                if initial_fen and initial_fen != 'startpos':
                    board = chess.Board(initial_fen, chess960=is_960)
                else:
                    board = chess.Board(chess960=is_960)

                clock     = state.get('clock', {})
                game_mode = 'chess960' if is_960 else _get_game_mode(clock)

                last_move_count = 0
                game_start_time = time.time()
                losing_msg_sent = False

                greeting_cat = "greeting_human" if is_vs_human else "greeting_bot"
                if not rated or SETTINGS.get("CHAT_IN_RATED", True):
                    _send_message(client, game_id, pick_message(greeting_cat))

                curr_state = state['state']

            elif state['type'] == 'gameState':
                curr_state = state
            else:
                continue

            if board is None: continue

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

            if (not game_started
                    and game_start_time
                    and (time.time() - game_start_time) > SETTINGS["ABORT_WAIT_SECONDS"]):
                try:
                    client.bots.abort_game(game_id)
                    print(f"⏳ Abort: {game_id} (rakip hamle yapmadı)")
                except Exception as e:
                    print(f"⚠️ Abort hatası: {e}")
                break

            status = curr_state.get('status')
            if status in ['mate', 'resign', 'draw', 'outoftime', 'aborted', 'stalemate']:
                winner       = curr_state.get('winner')
                my_color_str = 'white' if my_color == chess.WHITE else 'black'

                if status in ['draw', 'stalemate']:
                    result, msg_cat = 'draw', 'draw'
                elif winner:
                    result  = 'win' if winner == my_color_str else 'loss'
                    msg_cat = result
                else:
                    result, msg_cat = 'draw', 'draw'

                if not rated or SETTINGS.get("CHAT_IN_RATED", True):
                    _send_message(client, game_id, pick_message(msg_cat))
                if is_vs_human and (not rated or SETTINGS.get("CHAT_IN_RATED", True)):
                    time.sleep(1)
                    _send_message(client, game_id, pick_message("human_postgame"))

                if mm and status != 'aborted':
                    mm.record_game_result(result, game_mode, opponent_id=opp_id)
                break

            if (SETTINGS.get("SCORE_CHAT_ENABLED", False)
                    and is_vs_human and not losing_msg_sent
                    and len(board.move_stack) >= 20):
                try:
                    score = bot.get_score(board)
                    if score is not None:
                        my_score = score if my_color == chess.WHITE else -score
                        if my_score < SETTINGS["LOSING_SCORE_THRESHOLD"]:
                            _send_message(client, game_id, pick_message("losing_realization"))
                            losing_msg_sent = True
                except Exception as e:
                    print(f"⚠️ Skor hatası: {e}")

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


def handle_game_wrapper(game_id, bot, my_id, active_games, active_games_lock, mm):
    client = make_client()
    try:
        handle_game(client, game_id, bot, my_id, mm)
    finally:
        active_discard(active_games, active_games_lock, game_id)


def main():
    start_time = time.time()
    client     = make_client()

    try:
        with open("config.yml", "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        my_id = client.account.get()['id']
    except Exception as e:
        print(f"Bağlantı/Config Hatası: {e}")
        return

    if config and "matchmaking" in config:
        if "max_games" in config["matchmaking"]:
            SETTINGS["MAX_PARALLEL_GAMES"] = config["matchmaking"]["max_games"]

    bot = OxydanV11(
        SETTINGS["ENGINE_PATH"],
        uci_options=config.get('engine', {}).get('uci_options', {}) if config else {}
    )
    active_games = set()
    active_games_lock = threading.Lock()
    pending_starts = {"count": 0}

    mm = None
    if config and config.get("matchmaking"):
        mm = Matchmaker(
            client, config, active_games,
            token=SETTINGS["TOKEN"],
            active_games_lock=active_games_lock
        )
        threading.Thread(target=mm.start, daemon=True).start()

    threading.Thread(
        target=runtime_watchdog,
        args=(start_time, active_games, active_games_lock),
        daemon=True
    ).start()

    print(f"🔥 Oxydan 11 Hazır. ID: {my_id} | Watchdog Devrede.", flush=True)

    while True:
        try:
            for event in client.bots.stream_incoming_events():
                cur_elapsed    = time.time() - start_time
                time_remaining = SETTINGS["MAX_TOTAL_RUNTIME"] - cur_elapsed

                if event['type'] == 'challenge':
                    ch    = event['challenge']
                    ch_id = ch['id']

                    tc         = ch.get('timeControl', {})
                    time_limit = tc.get('limit', 0)
                    increment  = tc.get('increment', 0)

                    estimated_game_duration = (time_limit * 2) + (increment * 120)
                    is_time_safe = time_remaining > (
                        estimated_game_duration + SETTINGS["MIN_GAME_SECONDS_REMAINING"]
                    )

                    accept, reason = True, 'policy'
                    if mm:
                        accept, reason = mm.is_challenge_acceptable(ch)

                    can_accept = (
                        is_time_safe and
                        time_limit <= SETTINGS["MAX_GAME_TIME_LIMIT"] and
                        active_count(active_games, active_games_lock, pending_starts) < SETTINGS["MAX_PARALLEL_GAMES"] and
                        accept
                    )

                    try:
                        if can_accept:
                            if not reserve_game_slot(active_games, active_games_lock, pending_starts):
                                can_accept = False

                        if can_accept:
                            client.challenges.accept(ch_id)
                            print(
                                f"✅ Kabul: {ch_id} | {reason} | "
                                f"Kalan: {int(time_remaining)}s | "
                                f"Tahmini maç: {int(estimated_game_duration)}s",
                                flush=True
                            )
                        else:
                            if not is_time_safe:
                                detail = f"Oturum süresi yetersiz ({int(time_remaining)}s < {int(estimated_game_duration)}s)"
                            elif time_limit > SETTINGS["MAX_GAME_TIME_LIMIT"]:
                                detail = f"Oyun çok uzun ({time_limit}s)"
                            elif active_count(active_games, active_games_lock, pending_starts) >= SETTINGS["MAX_PARALLEL_GAMES"]:
                                detail = "Paralel maç limiti dolu"
                            else:
                                detail = reason

                            client.challenges.decline(ch_id, reason='later')
                            print(f"❌ Reddedildi: {ch_id} | {detail}", flush=True)

                    except Exception as ce:
                        if can_accept:
                            release_reserved_slot(active_games_lock, pending_starts)
                        print(f"⚠️ Challenge işleme hatası: {ce}", flush=True)

                elif event['type'] == 'gameStart':
                    game_id = event['game']['id']
                    release_reserved_slot(active_games_lock, pending_starts)
                    if active_add_if_room(active_games, active_games_lock, game_id):
                        threading.Thread(
                            target=handle_game_wrapper,
                            args=(game_id, bot, my_id, active_games, active_games_lock, mm),
                            daemon=True
                        ).start()

        except Exception as e:
            print(f"⚠️ Lichess akışı koptu, yeniden bağlanılıyor: {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
