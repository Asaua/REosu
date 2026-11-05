# -*- coding: utf-8 -*-
"""
REosu! - osu!stable 로컬 기록 복구 / 삭제 / 백업

osu!stable은 로컬 기록을 '게임을 정상 종료할 때' scores.db에 쓴다. 비정상 종료되면
그 세션 기록이 scores.db에 못 들어가지만, 리플레이(.osr)는 곡을 끝낼 때마다
Data/r에 바로 저장된다. 이 툴은 scores.db에 없는 리플레이를 찾아 기록으로 되살린다.

구조: exe가 127.0.0.1에 로컬 서버를 띄우고, 프로그램 자체 창(WebView2)에 web/index.html을 띄운다.
Edge 브라우저와 그 프로필은 쓰지 않는다. 창을 닫으면 프로그램도 종료된다.
"""
import base64
import ctypes
import datetime
import glob
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

APP_NAME = "REosu!"
AUTHOR_URL = "https://osu.ppy.sh/users/22386458"
KEEP_HISTORY = 20
OSU_EXIT_GRACE_SEC = 2.5  # osu!가 꺼진 뒤 파일을 다 쓸 때까지 기다리는 시간
MAX_BODY = 4 * 1024 * 1024
READ_THREADS = 24
READ_BATCH = 96
BACKUP_KEYS = ("scores", "collection", "songs", "skins", "screenshots", "cfg")
FONT_FILES = {"Outfit-Latin.woff2", "SpaceGrotesk-Latin.woff2"}
MD5_RE = re.compile(r"[0-9a-f]{32}")
LOOKUP_TIMEOUT = 8
STATUS_NAMES = {4: "Loved", 3: "Qualified", 2: "Approved", 1: "Ranked", 0: "Pending", -1: "WIP", -2: "Graveyard"}

# .NET DateTime ticks(0001-01-01)와 유닉스 시간(1970-01-01) / Windows FILETIME(1601-01-01)의 차이
UNIX_EPOCH_TICKS = 621355968000000000
FILETIME_OFFSET = 504911232000000000
# osu!가 Data/r에 저장하는 이름: {맵md5}-{FILETIME}.osr
REPLAY_NAME = re.compile(r"([0-9a-fA-F]{32})-(\d+)\.osr", re.I)

MOD_HIDDEN, MOD_FLASHLIGHT, MOD_FADEIN = 8, 1024, 1048576
SILVER_MODS = MOD_HIDDEN | MOD_FLASHLIGHT | MOD_FADEIN
MOD_TARGET = 1 << 23
MOD_NAMES = (
    (1, "NF"), (2, "EZ"), (8, "HD"), (16, "HR"), (32, "SD"), (64, "DT"), (128, "RX"), (256, "HT"),
    (512, "NC"), (1024, "FL"), (4096, "SO"), (16384, "PF"), (1048576, "FI"), (2097152, "RD"),
    (1073741824, "MR"),
)

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class RecoveryError(Exception):
    """사용자에게 그대로 보여 줄 오류."""


class OsuRunning(Exception):
    """처리 직전에 osu!가 다시 켜졌다. 작업을 대기열에 남겨 둔다."""


LANGS = ("ko", "en", "zh-CN", "ja")
_strings = {}
_lang = "en"


def load_strings():
    global _strings
    if not _strings:
        with open(os.path.join(resource_dir(), "web", "i18n.json"), encoding="utf-8") as f:
            _strings = json.load(f)
    return _strings


def system_lang():
    """Windows 표시 언어. 지원하지 않는 언어는 영어로 보여 준다."""
    try:
        primary = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
    except (AttributeError, OSError):
        return "en"
    return {0x12: "ko", 0x04: "zh-CN", 0x11: "ja"}.get(primary, "en")


def set_lang(setting):
    global _lang
    _lang = setting if setting in LANGS else system_lang()
    return _lang


def tr(key, **params):
    """번역 문구. 숫자는 천 단위로 쓰고, {s}는 영어 복수형에 쓴다."""
    strings = load_strings()
    text = strings.get(_lang, {}).get(key) or strings["en"].get(key, key)
    n = params.get("n")
    if isinstance(n, int):
        params["s"] = "" if n == 1 else "s"
    for k, v in params.items():
        text = text.replace("{" + k + "}", f"{v:,}" if isinstance(v, int) else str(v))
    return text.replace("{s}", "")


def resource_dir():
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def default_data_dir():
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "osu_score_recovery")


# ---------------------------------------------------------------------------
# 바이너리 파싱
# ---------------------------------------------------------------------------
class Reader:
    def __init__(self, data):
        self.b = data
        self.p = 0

    def read(self, fmt):
        v = struct.unpack_from("<" + fmt, self.b, self.p)[0]
        self.p += struct.calcsize("<" + fmt)
        return v

    def skip(self, n):
        self.p += n

    def string(self):
        flag = self.b[self.p]
        self.p += 1
        if flag == 0x00:
            return None
        if flag != 0x0B:
            raise ValueError(f"bad string flag 0x{flag:02x} at {self.p - 1}")
        length = shift = 0
        while True:
            c = self.b[self.p]
            self.p += 1
            length |= (c & 0x7F) << shift
            shift += 7
            if not c & 0x80:
                break
        s = self.b[self.p:self.p + length].decode("utf-8", "replace")
        self.p += length
        return s


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def add_scores(beatmaps, rows):
    """beatmaps에 기록을 넣고, osu!처럼 맵별 점수 내림차순으로 맞춘다."""
    index = {md5: i for i, (md5, _, _) in enumerate(beatmaps)}
    for s in rows:
        item = {"raw": s["raw"], "score": s["score"], "md5": s["md5"], "ts": s["ts"]}
        if s["md5"] in index:
            beatmaps[index[s["md5"]]][2].append(item)
        else:
            index[s["md5"]] = len(beatmaps)
            beatmaps.append((s["md5"], osu_string(s["md5"]), [item]))
    for _, _, ss in beatmaps:
        ss.sort(key=lambda s: s["score"], reverse=True)


def osu_string(s):
    if s is None:
        return b"\x00"
    b = s.encode("utf-8")
    out, n = bytearray(b"\x0b"), len(b)
    while True:
        c = n & 0x7F
        n >>= 7
        out.append(c | (0x80 if n else 0))
        if not n:
            return bytes(out + b)


def parse_scores_data(data):
    """scores.db 바이트 -> (version, [(md5, md5_raw, [score, ...]), ...])
    score는 원본 바이트를 그대로 들고 있어 다시 쓸 때 손실이 없다."""
    r = Reader(data)
    version = r.read("i")
    beatmaps = []
    for _ in range(r.read("i")):
        start = r.p
        md5 = r.string()
        md5_raw = data[start:r.p]
        scores = []
        for _ in range(r.read("i")):
            s0 = r.p
            r.read("B")  # mode
            r.read("i")  # score version
            bm_md5 = r.string()
            r.string()  # player
            r.string()  # replay md5
            r.skip(12)  # 판정 수 6개
            score = r.read("i")
            r.read("h")  # max combo
            r.read("?")  # perfect
            mods = r.read("i")
            r.string()  # life bar graph (항상 비어 있음)
            ts = r.read("q")
            r.read("i")  # -1
            r.read("q")  # online score id
            if mods & MOD_TARGET:
                r.read("d")
            scores.append({"raw": data[s0:r.p], "md5": bm_md5 or md5, "score": score, "ts": ts})
        beatmaps.append((md5, md5_raw, scores))
    if r.p != len(data):
        raise ValueError(f"length mismatch ({r.p} != {len(data)})")
    return version, beatmaps


def read_scores_db(path):
    """(version, beatmaps). 파일이 없으면 (None, []). 깨져 있으면 덮어쓰지 않도록 오류를 낸다."""
    if not os.path.isfile(path):
        return None, []
    with open(path, "rb") as f:
        data = f.read()
    try:
        return parse_scores_data(data)
    except (ValueError, IndexError, struct.error) as e:
        raise RecoveryError(tr("srv.dbUnreadable", x=e))


def build_scores_data(version, beatmaps):
    out = bytearray(struct.pack("<ii", version, len(beatmaps)))
    for _md5, md5_raw, scores in beatmaps:
        out += md5_raw
        out += struct.pack("<i", len(scores))
        for s in scores:
            out += s["raw"]
    return bytes(out)


def write_scores_db(path, version, beatmaps):
    data = build_scores_data(version, beatmaps)
    # 디스크에 쓰기 전에 다시 읽어 보고, 개수가 맞을 때만 교체한다.
    _, check = parse_scores_data(data)
    if sum(len(b[2]) for b in check) != sum(len(b[2]) for b in beatmaps):
        raise RecoveryError(tr("srv.verifyFail"))
    tmp = path + ".tmp_recovery"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def iter_osu_db(data, strict=False):
    """osu!.db -> (md5, '아티스트 - 제목 [난이도]', 등급 값의 위치, 안 해 봄/마지막 플레이 값의 위치)를 맵마다 낸다."""
    r = Reader(data)
    ver = r.read("i")
    r.read("i")  # folder count
    r.read("?")
    r.read("q")
    r.string()  # player name
    for _ in range(r.read("i")):
        if ver < 20191106:
            r.read("i")
        artist = r.string()
        r.string()
        title = r.string()
        r.string()
        r.string()  # creator
        diff = r.string()
        r.string()  # audio
        md5 = r.string()
        r.string()  # .osu file
        r.skip(1 + 2 * 3 + 8)
        r.skip(4 if ver < 20140609 else 16)
        r.skip(8)  # slider velocity
        if ver >= 20140609:
            pair = 10 if ver >= 20250107 else 14
            for _ in range(4):
                r.skip(r.read("i") * pair)
        r.skip(12)  # drain / total / preview
        r.skip(r.read("i") * 17)  # timing points
        r.skip(12)  # difficulty id / set id / thread id
        grade_at = r.p
        r.skip(4)  # 모드별 등급
        r.skip(2 + 4 + 1)
        r.string()  # source
        r.string()  # tags
        r.skip(2)
        r.string()  # title font
        played_at = r.p
        r.skip(1 + 8 + 1)  # unplayed / last played / osz2
        r.string()  # folder
        r.skip(8 + 5)
        if ver < 20140609:
            r.skip(2)
        r.skip(4 + 1)
        yield md5, f"{artist} - {title} [{diff}]", grade_at, played_at
    if strict and len(data) - r.p != 4:  # 고치기 전에는 끝까지 정확히 맞아야 위치를 믿을 수 있다
        raise ValueError("unexpected osu!.db layout")


def read_map_names(path):
    """osu!.db -> {md5: '아티스트 - 제목 [난이도]'}. 읽지 못해도 복구는 할 수 있으므로 None을 돌려준다."""
    try:
        with open(path, "rb") as f:
            return {row[0]: row[1] for row in iter_osu_db(f.read())}
    except (OSError, ValueError, IndexError, struct.error):
        return None


PLAYED = struct.Struct("<?q")  # 안 해 봄, 마지막 플레이 (.NET ticks)
GRADE_LETTERS = ("SS", "S", "A", "B", "C", "D")
GRADE_BYTES = {"SS": 2, "S": 3, "A": 4, "B": 5, "C": 6, "D": 7}
SILVER_BYTES = {"SS": 0, "S": 1}
GRADE_NONE = 9


def best_grades(beatmaps, changed):
    """{(md5, 모드)} -> {md5: {모드: 등급 값}}. 남은 기록 중 가장 높은 등급, 같으면 은색이 위. 없으면 비움."""
    md5s = {md5 for md5, _ in changed}
    best = {}
    for _md5, _raw, ss in beatmaps:
        for s in ss:
            if s["md5"] not in md5s:
                continue
            e = parse_score_entry(s["raw"])
            key = (e["md5"], e["mode"])
            if key not in changed:
                continue
            letter = judge(e["mode"], e["counts"], e["mods"])[0]
            silver = letter in SILVER_BYTES and bool(e["mods"] & SILVER_MODS)
            rank = (GRADE_LETTERS.index(letter), not silver)
            if key not in best or rank < best[key][0]:
                best[key] = (rank, SILVER_BYTES[letter] if silver else GRADE_BYTES[letter])
    out = {}
    for md5, mode in changed:
        out.setdefault(md5, {})[mode] = best[(md5, mode)][1] if (md5, mode) in best else GRADE_NONE
    return out


def update_osu_db(osu_dir, beatmaps, changed, played=None):
    """기록이 바뀐 맵의 등급과 마지막 플레이 값만 제자리에서 고친다. changed는 {(md5, 모드)}이다.
    played(md5, 지금 값) -> 새 값, 그대로면 None. 0은 플레이한 적 없음이다.
    osu!가 꺼져 있을 때만 부른다. 고치지 못하면 False."""
    path = os.path.join(osu_dir, "osu!.db")
    try:
        grades = best_grades(beatmaps, changed)
        with open(path, "rb") as f:
            data = f.read()
        writes = []
        for md5, _name, grade_at, played_at in iter_osu_db(data, strict=True):
            for mode, value in grades.get(md5, {}).items():
                if data[grade_at + mode] != value:
                    writes.append((grade_at + mode, bytes([value])))
            new = played(md5, PLAYED.unpack_from(data, played_at)[1]) if played else None
            if new is not None:
                writes.append((played_at, PLAYED.pack(new == 0, new)))
        if writes:
            with open(path, "r+b") as f:
                for at, value in writes:
                    f.seek(at)
                    f.write(value)
        return True
    except (OSError, ValueError, IndexError, struct.error):
        return False


def parse_osr(path):
    """.osr 헤더와 끝부분만 읽어 scores.db 항목 바이트를 만든다 (리플레이 본문은 건너뜀)."""
    with open(path, "rb") as f:
        head = f.read(8192)
        try:
            return _parse_osr(f, head)
        except (IndexError, struct.error):
            # 체력 그래프 문자열이 길면 헤더가 8KB를 넘는다 -> 파일 전체로 다시 시도
            f.seek(0)
            return _parse_osr(f, f.read())


def _parse_osr(f, head):
    r = Reader(head)
    r.read("B")  # mode
    r.read("i")  # version
    r.string()  # md5
    r.string()  # player
    r.string()  # replay md5
    r.skip(12 + 4 + 2 + 1)  # 판정 수 / 점수 / 콤보 / perfect
    mods = r.read("i")
    life_off = r.p
    r.string()  # life bar graph: scores.db에는 빈 문자열로 저장된다
    ts = r.read("q")
    n = r.read("i")
    f.seek(r.p + n)
    tail = f.read(16)
    need = 16 if mods & MOD_TARGET else 8
    if len(tail) < need:
        raise ValueError("not an osu! replay")
    return parse_score_entry(head[:life_off] + b"\x00" + struct.pack("<qi", ts, -1) + tail[:need])


def parse_score_entry(raw):
    """scores.db 항목 바이트 -> 표시와 복구에 쓰는 필드."""
    r = Reader(raw)
    mode = r.read("B")
    version = r.read("i")
    md5 = r.string()
    player = r.string()
    r.string()  # replay md5
    counts = tuple(r.read("h") for _ in range(6))
    score = r.read("i")
    combo = r.read("h")
    r.read("?")
    mods = r.read("i")
    r.string()
    ts = r.read("q")
    if mode > 3 or not md5:
        raise ValueError("not an osu! replay")
    return {"raw": raw, "mode": mode, "version": version, "md5": md5, "player": player, "counts": counts,
            "score": score, "combo": combo, "mods": mods, "ts": ts}


def judge(mode, counts, mods):
    """(등급 글자, 정확도 0~1)"""
    c300, c100, c50, geki, katu, miss = counts
    if mode in (0, 1):
        total = c300 + c100 + c50 + miss
        if total <= 0:
            return "D", 0.0
        r300, r50 = c300 / total, c50 / total
        if mode == 0:
            acc = (300 * c300 + 100 * c100 + 50 * c50) / (300 * total)
        else:
            acc = (c300 + 0.5 * c100) / total
        if r300 == 1:
            g = "SS"
        elif r300 > 0.9 and r50 <= 0.01 and miss == 0:
            g = "S"
        elif (r300 > 0.8 and miss == 0) or r300 > 0.9:
            g = "A"
        elif (r300 > 0.7 and miss == 0) or r300 > 0.8:
            g = "B"
        elif r300 > 0.6:
            g = "C"
        else:
            g = "D"
    else:
        if mode == 2:
            total = c300 + c100 + c50 + katu + miss
            acc = (c300 + c100 + c50) / total if total else 0.0
            cuts = (0.98, 0.94, 0.90, 0.85)
        else:
            total = c300 + c100 + c50 + geki + katu + miss
            acc = (300 * (c300 + geki) + 200 * katu + 100 * c100 + 50 * c50) / (300 * total) if total else 0.0
            cuts = (0.95, 0.90, 0.80, 0.70)
        g = "SS" if acc >= 1 else "D"
        if g == "D":
            for letter, cut in zip("SABC", cuts):
                if acc > cut:
                    g = letter
                    break
    return g, acc


def mod_list(mods):
    names = [name for bit, name in MOD_NAMES if mods & bit]
    if "NC" in names and "DT" in names:
        names.remove("DT")
    if "PF" in names and "SD" in names:
        names.remove("SD")
    return names


def format_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "REosu"})
    with urllib.request.urlopen(req, timeout=LOOKUP_TIMEOUT) as res:
        return json.loads(res.read(2 * 1024 * 1024))


def _to_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _map_info(source, artist, title, version, creator, set_id, map_id, status, stars, bpm, length):
    return {"found": True, "source": source, "artist": str(artist or ""), "title": str(title or ""), "version": str(version or ""),
            "creator": str(creator or ""), "set_id": _to_int(set_id), "map_id": _to_int(map_id),
            "status": STATUS_NAMES.get(_to_int(status, 99), ""), "stars": round(_to_float(stars), 2),
            "bpm": round(_to_float(bpm), 1), "length": _to_int(length)}


def lookup_online(md5):
    """맵 해시로 공개 미러를 조회한다. 없으면 None, 모두 접속 실패면 OSError."""
    failures = 0
    try:
        rows = _fetch_json(f"https://osu.direct/api/get_beatmaps?h={md5}")
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            b = rows[0]
            return _map_info("osu.direct", b.get("artist"), b.get("title"), b.get("version"), b.get("creator"),
                             b.get("beatmapset_id"), b.get("beatmap_id"), b.get("approved"),
                             b.get("difficultyrating"), b.get("bpm"), b.get("total_length"))
        if isinstance(rows, list):
            return None
    except urllib.error.HTTPError as e:
        if e.code != 404:
            failures += 1
    except (OSError, ValueError):
        failures += 1
    try:
        b = _fetch_json(f"https://catboy.best/api/v2/md5/{md5}")
        if isinstance(b, dict) and b.get("id"):
            bset = b.get("set") if isinstance(b.get("set"), dict) else {}
            return _map_info("catboy.best", bset.get("artist"), bset.get("title"), b.get("version"), bset.get("creator"),
                             b.get("beatmapset_id") or bset.get("id"), b.get("id"), b.get("ranked"),
                             b.get("difficulty_rating"), b.get("bpm"), b.get("total_length"))
        return None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        failures += 1
    except (OSError, ValueError):
        failures += 1
    if failures:
        raise OSError(tr("srv.lookupServer"))
    return None


def online_name(info):
    return f"{info['artist']} - {info['title']} [{info['version']}]" if info.get("title") else info.get("version") or None


def public_entry(i, s):
    grade, acc = judge(s["mode"], s["counts"], s["mods"])
    return {
        "id": i, "t": (s["ts"] - UNIX_EPOCH_TICKS) // 10000, "name": s["name"], "md5": s["md5"],
        "player": s["player"] or "", "score": s["score"], "combo": s["combo"], "mode": s["mode"],
        "grade": grade, "silver": bool(s["mods"] & SILVER_MODS),
        "acc": round(acc, 5), "mods": mod_list(s["mods"]), "counts": list(s["counts"]),
        "ts": str(s["ts"]),  # 자바스크립트 숫자로는 정밀도가 모자라 문자열로 보낸다
    }


# ---------------------------------------------------------------------------
# osu! 폴더 탐지 / 프로세스 제어
#   stable과 lazer는 프로세스 이름이 둘 다 osu!.exe다. 다른 osu!를 끄지 않도록
#   실행 파일 경로가 선택한 폴더인 프로세스만 다룬다.
# ---------------------------------------------------------------------------
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
_kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD))


def process_path(pid):
    handle = _kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        _kernel32.CloseHandle(handle)


def all_osu_pids():
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq osu!.exe", "/FO", "CSV", "/NH"],
        capture_output=True, creationflags=NO_WINDOW,
    ).stdout.decode("mbcs", "replace")
    pids = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "osu!.exe" and parts[1].isdigit():
            pids.append(int(parts[1]))
    return pids


def same_dir(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def osu_pids(osu_dir):
    """선택한 폴더에서 실행 중인 osu!.exe의 PID."""
    return [pid for pid in all_osu_pids()
            if (path := process_path(pid)) and same_dir(os.path.dirname(path), osu_dir)]


def is_stable_dir(d):
    return bool(d) and os.path.isfile(os.path.join(d, "osu!.exe")) and (
        os.path.isfile(os.path.join(d, "scores.db")) or os.path.isfile(os.path.join(d, "osu!.db")))


def registry_osu_dirs():
    import winreg
    dirs = []
    for key in ("osustable.File.osz", "osustable.File.osr", "osu!", "osu"):
        try:
            cmd = winreg.QueryValue(winreg.HKEY_CLASSES_ROOT, key + r"\shell\open\command")
        except OSError:
            continue
        m = re.search(r'"?([^"]*osu!\.exe)"?', cmd, re.I)
        if m:
            dirs.append(os.path.dirname(m.group(1)))
    return dirs


def detect_osu_dirs():
    """찾은 osu!stable 폴더 전부. 실행 중인 것과 레지스트리에 있는 것이 앞에 온다."""
    cands = [os.path.dirname(p) for p in map(process_path, all_osu_pids()) if p]
    cands += registry_osu_dirs()
    cands.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "osu!"))
    for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if root:
            cands.append(os.path.join(root, "osu!"))
    cands += [rf"{letter}:\osu!" for letter in "CDEFG"]
    found = []
    for d in cands:
        if is_stable_dir(d) and not any(same_dir(d, f) for f in found):
            found.append(os.path.normpath(d))
    return found


def detect_osu_dir():
    found = detect_osu_dirs()
    return found[0] if found else ""


def songs_dir(osu_dir):
    """Songs 폴더. 위치를 옮긴 경우 cfg의 BeatmapDirectory를 따른다 (이 키 외의 값은 읽지 않는다)."""
    user_cfg = os.path.join(osu_dir, f"osu!.{os.environ.get('USERNAME', '')}.cfg")
    paths = [user_cfg] + sorted(glob.glob(os.path.join(glob.escape(osu_dir), "osu!.*.cfg")))
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    key, sep, value = line.partition("=")
                    if sep and key.strip() == "BeatmapDirectory" and value.strip():
                        return os.path.normpath(os.path.join(osu_dir, value.strip()))
        except OSError:
            continue
    return os.path.join(osu_dir, "Songs")


def backup_sources(osu_dir, key):
    """백업 항목 -> [(원본 경로, 백업 폴더 안 상대 경로)]"""
    if key == "scores":
        return [(os.path.join(osu_dir, "scores.db"), "scores.db"),
                (os.path.join(osu_dir, "Data", "r"), os.path.join("Data", "r"))]
    if key == "collection":
        return [(os.path.join(osu_dir, "collection.db"), "collection.db")]
    if key == "songs":
        return [(songs_dir(osu_dir), "Songs")]
    if key == "skins":
        return [(os.path.join(osu_dir, "Skins"), "Skins")]
    if key == "screenshots":
        return [(os.path.join(osu_dir, "Screenshots"), "Screenshots")]
    if key == "cfg":
        return [(p, os.path.basename(p)) for p in sorted(glob.glob(os.path.join(glob.escape(osu_dir), "osu!*.cfg")))]
    raise ValueError(key)


def long_path(path):
    """260자를 넘는 곡 폴더 경로도 다룰 수 있게 한다."""
    path = os.path.abspath(path)
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def iter_files(src, rel):
    """(원본 파일, 상대 경로, 크기). 하위 폴더까지 돈다. 링크 폴더는 따라가지 않는다."""
    src = long_path(src)
    if os.path.isfile(src):
        yield src, rel, os.path.getsize(src)
        return
    stack = [(src, rel)]
    while stack:
        folder, sub = stack.pop()
        try:
            with os.scandir(folder) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append((e.path, os.path.join(sub, e.name)))
                elif e.is_file(follow_symlinks=False):
                    yield e.path, os.path.join(sub, e.name), e.stat(follow_symlinks=False).st_size
            except OSError:
                continue


def is_inside(child, parent):
    a, b = (os.path.normcase(os.path.abspath(x)) for x in (child, parent))
    try:
        return os.path.commonpath([a, b]) == b
    except ValueError:  # 드라이브가 다름
        return False


def pick_folder(initial, title):
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        path = filedialog.askdirectory(parent=root, initialdir=initial or None, title=title)
    finally:
        root.destroy()
    return os.path.normpath(path) if path else None


# ---------------------------------------------------------------------------
# 앱 상태 / 작업
# ---------------------------------------------------------------------------
class App:
    def __init__(self, data_dir=None, osu_dir=None, replay_dir=None):
        self.data_dir = data_dir or default_data_dir()
        self.history_root = os.path.join(self.data_dir, "history")
        self.cfg_path = os.path.join(self.data_dir, "config.json")
        self.cache_path = os.path.join(self.data_dir, "replay_cache.json")
        self.map_path = os.path.join(self.data_dir, "map_lookup.json")
        self.queue_path = os.path.join(self.data_dir, "queue.json")
        os.makedirs(self.history_root, exist_ok=True)

        cfg = self.load_config()
        self.osu_dir = osu_dir or cfg.get("osu_dir") or ""
        if not osu_dir and not is_stable_dir(self.osu_dir):
            self.osu_dir = detect_osu_dir()
        self.replay_dir = replay_dir or cfg.get("replay_dir") or ""
        self.drag_select = bool(cfg.get("drag_select", False))
        self.onboarded = bool(cfg.get("onboarded", False))
        self.lang_setting = cfg.get("lang") or "auto"
        self.lang = set_lang(self.lang_setting)
        self.trash_dir = cfg.get("trash_dir") or ""
        self.trash_error = False
        self.backup_dir = cfg.get("backup_dir") or ""
        self.last_backup = None
        self.sizes = {}
        self.size_gen = 0
        self.names = None

        self.maps = self.load_maps()
        self.queue = self.load_queue()
        self.queue_rev = 0
        self.last_running = 0.0
        self.lock = threading.Lock()
        self.pick_lock = threading.Lock()
        self.names_lock = threading.Lock()
        self.job = None
        self.last = None
        self.seq = 0
        self.osu_running = False
        self.cands = {}
        self.stats = None
        self.next_id = 1
        self.cancel = False
        self.clients = 0
        self.ever_connected = False
        self.last_seen = time.time()

    # --- 설정 ---
    def load_config(self):
        try:
            with open(self.cfg_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_config(self):
        tmp = self.cfg_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"osu_dir": self.osu_dir, "replay_dir": self.replay_dir, "trash_dir": self.trash_dir,
                       "backup_dir": self.backup_dir, "lang": self.lang_setting,
                       "drag_select": self.drag_select, "onboarded": self.onboarded}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.cfg_path)

    def replay_dirs(self, osu_dir):
        dirs = [os.path.join(osu_dir, "Data", "r")]
        if self.replay_dir and not same_dir(self.replay_dir, dirs[0]):
            dirs.append(self.replay_dir)
        return dirs

    def trash_root(self, osu_dir):
        return self.trash_dir or os.path.join(osu_dir, "Data", "r", "_deleted")

    def effective_replay_dir(self):
        return self.replay_dir or (os.path.join(self.osu_dir, "Data", "r") if self.osu_dir else "")

    def config_view(self):
        try:
            free = shutil.disk_usage(self.backup_dir).free if self.backup_dir else None
        except OSError:
            free = None
        return {
            "osu_dir": self.osu_dir, "replay_dir": self.replay_dir, "replay_default": self.effective_replay_dir(),
            "valid": is_stable_dir(self.osu_dir), "trash_dir": self.trash_dir,
            "trash_default": os.path.join(self.osu_dir, "Data", "r", "_deleted") if self.osu_dir else "",
            "trash_error": self.trash_error, "lang": self.lang_setting, "lang_effective": self.lang,
            "drag_select": self.drag_select, "onboarded": self.onboarded,
            "lazer": os.path.isfile(os.path.join(self.osu_dir, "client.realm")) if self.osu_dir else False,
            "osu_found": detect_osu_dirs() if not self.onboarded else [],
            "backup_dir": self.backup_dir, "backup_free": free,
        }

    def set_config(self, body):
        if "osu_dir" in body:
            new = os.path.normpath(body["osu_dir"].strip()) if body["osu_dir"].strip() else ""
            if new != self.osu_dir:
                self.osu_dir = new
                self.replay_dir = ""
                self.cands, self.stats, self.names = {}, None, None
                self.size_gen += 1
                self.sizes = {}
        if "replay_dir" in body:
            new = os.path.normpath(body["replay_dir"].strip()) if body["replay_dir"].strip() else ""
            if new != self.replay_dir:
                self.replay_dir = new
                self.cands, self.stats = {}, None
        if "onboarded" in body:
            self.onboarded = bool(body["onboarded"])
        if "drag_select" in body:
            self.drag_select = bool(body["drag_select"])
        if "lang" in body:
            self.lang_setting = body["lang"] if body["lang"] in LANGS else "auto"
            self.lang = set_lang(self.lang_setting)
        if "trash_dir" in body:
            new = os.path.normpath(body["trash_dir"].strip()) if body["trash_dir"].strip() else ""
            # 리플레이 폴더 자체에 두면 지운 기록이 복구 후보로 다시 나타난다
            self.trash_error = bool(new) and any(
                same_dir(new, d) for d in self.replay_dirs(self.osu_dir))
            if not self.trash_error:
                self.trash_dir = new
        if "backup_dir" in body:
            self.backup_dir = os.path.normpath(body["backup_dir"].strip()) if body["backup_dir"].strip() else ""
        self.save_config()

    # --- 설치되지 않은 맵 정보 ---
    def load_maps(self):
        try:
            with open(self.map_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def lookup_map(self, md5):
        if not MD5_RE.fullmatch(md5 or ""):
            return {"error": tr("srv.badRequest")}
        info = self.maps.get(md5)
        if info:
            return info
        try:
            info = lookup_online(md5)
        except OSError as e:
            return {"error": str(e)}
        if info is None:
            return {"found": False}
        with self.lock:
            self.maps[md5] = info
            tmp = self.map_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.maps, f, ensure_ascii=False)
            os.replace(tmp, self.map_path)
        return info

    def entry(self, i, s):
        e = public_entry(i, s)
        info = None if e["name"] else self.maps.get(e["md5"])
        e["online"] = online_name(info) if info else None
        e["set_id"] = info.get("set_id", 0) if info else 0
        e["map_id"] = info.get("map_id", 0) if info else 0
        return e

    # --- 작업 실행 ---
    def snapshot(self):
        with self.lock:
            counts = {}
            for op in self.queue:
                counts[op["kind"]] = counts.get(op["kind"], 0) + op["count"]
            return {"osu": self.osu_running, "job": self.job, "last": self.last, "sizes": self.sizes,
                    "queue": counts, "queue_rev": self.queue_rev}

    def set_progress(self, **kw):
        with self.lock:
            if self.job:
                self.job.update(kw)

    def start_job(self, kind, fn):
        with self.lock:
            if self.job:
                return False
            self.job = {"kind": kind, "done": 0, "total": 0, "msg": ""}

        def run():
            try:
                msg = fn()
                ok = True
            except RecoveryError as e:
                msg, ok = str(e), False
            except Exception as e:
                msg, ok = tr("srv.error", x=e), False
            extra = {}
            if isinstance(msg, dict):
                extra, msg = msg, msg["msg"]
            with self.lock:
                self.seq += 1
                self.last = {**extra, "seq": self.seq, "kind": kind, "ok": ok, "msg": msg}
                self.job = None
        threading.Thread(target=run, daemon=True).start()
        return True

    def require_osu_dir(self):
        if not is_stable_dir(self.osu_dir):
            if os.path.isfile(os.path.join(self.osu_dir, "client.realm")):
                raise RecoveryError(tr("srv.lazer"))
            raise RecoveryError(tr("srv.noOsu"))
        return self.osu_dir

    # --- 검색 ---
    #   HDD에서는 파일을 여는 시간이 대부분이라 처음 검색이 몇 분 걸릴 수 있다. 그래서
    #   이름으로 이미 있는 기록을 거르고, 최신 파일부터 읽어 찾는 대로 목록에 보여 주며,
    #   읽은 결과는 저장해 다음 검색은 파일을 열지 않는다.
    def job_scan(self):
        osu_dir = self.require_osu_dir()
        folder = self.effective_replay_dir()
        if not os.path.isdir(folder):
            raise RecoveryError(tr("srv.noReplayDir", x=folder))
        self.cancel = False
        with self.lock:
            self.cands, self.stats = {}, None
        self.set_progress(msg=tr("srv.readScores"))
        _, beatmaps = read_scores_db(os.path.join(osu_dir, "scores.db"))
        have = {(s["md5"], s["ts"]) for _, _, ss in beatmaps for s in ss}
        self.set_progress(msg=tr("srv.readMaps"))
        with self.names_lock:
            names = self.names = read_map_names(os.path.join(osu_dir, "osu!.db"))

        files, skipped = [], 0
        with os.scandir(folder) as it:
            for e in it:
                if not e.name.lower().endswith(".osr") or not e.is_file():
                    continue
                m = REPLAY_NAME.fullmatch(e.name)
                ts = int(m.group(2)) + FILETIME_OFFSET if m else 0
                if m and (m.group(1).lower(), ts) in have:
                    skipped += 1
                    continue
                files.append((ts, e.path, e.stat().st_size))
        files.sort(key=lambda x: x[0], reverse=True)

        with self.lock:
            self.stats = {"files": len(files) + skipped, "bad": 0, "existing": len(have),
                          "names_ok": names is not None, "folder": folder, "partial": False}
        self.set_progress(total=len(files), done=0, msg=tr("srv.checkReplays"))

        cache = self.load_cache()
        seen, bad, done = set(), 0, 0

        def read(item):
            try:
                return item, parse_osr(item[1])
            except (OSError, ValueError, struct.error, IndexError):
                return item, None

        def accept(s, path):
            key = (s["md5"], s["ts"])
            if key in have or key in seen:
                return
            seen.add(key)
            s["path"] = path
            s["name"] = names.get(s["md5"]) if names else None
            with self.lock:
                self.cands[self.next_id] = s
                self.next_id += 1

        # 저장해 둔 것은 먼저 넣고, 나머지만 디스크에서 읽는다.
        todo = []
        for item in files:
            hit = cache.get(item[1])
            s = None
            if hit and hit[0] == item[2]:
                try:
                    s = parse_score_entry(base64.b64decode(hit[1]))
                except (ValueError, IndexError, struct.error):
                    s = None
            if s:
                accept(s, item[1])
                done += 1
            else:
                todo.append(item)
        self.set_progress(done=done)

        saved_at = time.time()
        with ThreadPoolExecutor(READ_THREADS) as pool:
            for i in range(0, len(todo), READ_BATCH):
                if self.cancel:
                    break
                for item, s in pool.map(read, todo[i:i + READ_BATCH]):
                    done += 1
                    if s is None:
                        bad += 1
                        continue
                    cache[item[1]] = [item[2], base64.b64encode(s["raw"]).decode("ascii")]
                    accept(s, item[1])
                self.set_progress(done=done)
                if time.time() - saved_at > 20:
                    self.save_cache(cache)
                    saved_at = time.time()
        self.save_cache(cache)

        partial = self.cancel and done < len(files)
        with self.lock:
            self.stats.update(bad=bad, partial=partial, read=done, total=len(files))
        if partial:
            return tr("srv.scanStopped", n=len(self.cands))
        return tr("srv.scanDone", n=len(files) + skipped)

    def load_cache(self):
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_cache(self, cache):
        tmp = self.cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, self.cache_path)

    def public_candidates(self, since=0):
        with self.lock:
            rows = [(i, s) for i, s in self.cands.items() if i > since]
            stats = dict(self.stats) if self.stats else None
        return {"stats": stats, "items": [self.entry(i, s) for i, s in rows]}

    # --- 전체 기록: scores.db에 들어 있는 기록 ---
    def public_saved(self):
        if not is_stable_dir(self.osu_dir):
            return []
        _, beatmaps = read_scores_db(os.path.join(self.osu_dir, "scores.db"))
        with self.names_lock:  # 검색과 동시에 읽지 않도록 한 번만 읽는다
            if self.names is None:
                self.names = read_map_names(os.path.join(self.osu_dir, "osu!.db"))
        rows, i = [], 0
        for md5, _, ss in beatmaps:
            for s in ss:
                try:
                    e = parse_score_entry(s["raw"])
                except (ValueError, IndexError, struct.error):
                    continue
                i -= 1  # 복구 후보(양수)와 겹치지 않는 번호
                e["name"] = self.names.get(e["md5"]) if self.names else None
                rows.append(self.entry(i, e))
        return rows

    # --- 백업 ---
    def measure_sizes(self):
        """백업 항목 크기를 뒤에서 잰다. Songs는 HDD에서 오래 걸려 화면을 막지 않는다."""
        if not is_stable_dir(self.osu_dir):
            return
        with self.lock:
            if self.sizes:
                return
            self.size_gen += 1
            gen = self.size_gen
            self.sizes = {k: {"bytes": 0, "files": 0, "done": False} for k in BACKUP_KEYS}
        osu_dir = self.osu_dir

        def work():
            for key in BACKUP_KEYS:
                total = files = 0
                try:
                    for src, rel in backup_sources(osu_dir, key):
                        for _path, _rel, size in iter_files(src, rel):
                            total += size
                            files += 1
                            if files % 500 == 0:
                                if gen != self.size_gen:
                                    return
                                with self.lock:
                                    self.sizes[key] = {"bytes": total, "files": files, "done": False}
                except OSError:
                    pass
                with self.lock:
                    if gen != self.size_gen:
                        return
                    self.sizes[key] = {"bytes": total, "files": files, "done": True}
        threading.Thread(target=work, daemon=True).start()

    def job_backup(self, keys):
        osu_dir = self.require_osu_dir()
        keys = [k for k in BACKUP_KEYS if k in keys]
        if not keys:
            raise RecoveryError(tr("srv.backupNoItems"))
        dest = self.backup_dir
        if not dest or not os.path.isdir(dest):
            raise RecoveryError(tr("srv.backupNoDest"))
        sources = [item for k in keys for item in backup_sources(osu_dir, k)]
        for src, _rel in sources + [(osu_dir, "")]:
            if is_inside(dest, src):
                raise RecoveryError(tr("srv.backupInside"))

        self.cancel = False
        self.set_progress(msg=tr("srv.listFiles"))
        files = []
        for src, rel in sources:
            for item in iter_files(src, rel):
                files.append(item)
                if self.cancel:
                    return tr("srv.backupStopEmpty")
        total = sum(f[2] for f in files)
        free = shutil.disk_usage(dest).free
        if total > free:
            raise RecoveryError(tr("srv.noSpace", a=format_bytes(total), b=format_bytes(free)))

        target = os.path.join(dest, "osu_backup_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        os.makedirs(target)
        self.last_backup = target
        self.set_progress(msg="", total=total, done=0, files=0, files_total=len(files))

        copied, done, errors, tick = 0, 0, [], time.time()
        for path, rel, size in files:
            if self.cancel:
                break
            out = long_path(os.path.join(target, rel))
            try:
                os.makedirs(os.path.dirname(out), exist_ok=True)
                shutil.copy2(path, out)
                if os.path.getsize(out) != size and os.path.getsize(path) == size:
                    errors.append(f"{rel}\t{tr('srv.sizeDiff')}")
                else:
                    copied += 1
            except OSError as e:
                if getattr(e, "winerror", None) in (39, 112) or e.errno == 28:  # 디스크 가득 참
                    errors.append(f"{rel}\t{tr('srv.diskFullFile')}")
                    self.write_backup_info(target, osu_dir, keys, copied, done, errors, False)
                    raise RecoveryError(tr("srv.diskFull"))
                errors.append(f"{rel}\t{e.strerror or e}")
            done += size
            if time.time() - tick > 0.2:
                self.set_progress(done=done, files=copied)
                tick = time.time()
        finished = not self.cancel
        self.write_backup_info(target, osu_dir, keys, copied, done, errors, finished)
        if not finished:
            return tr("srv.backupStopped", n=copied)
        msg = tr("srv.backupDone", n=copied, x=format_bytes(done))
        if errors:
            msg += tr("srv.backupErrors", n=len(errors))
        return msg

    @staticmethod
    def write_backup_info(target, osu_dir, keys, copied, size, errors, complete):
        with open(os.path.join(target, "backup_info.json"), "w", encoding="utf-8") as f:
            json.dump({"time": datetime.datetime.now().isoformat(timespec="seconds"), "osu_dir": osu_dir,
                       "items": keys, "files": copied, "bytes": size, "errors": len(errors), "complete": complete},
                      f, ensure_ascii=False, indent=2)
        if errors:
            with open(os.path.join(target, "backup_errors.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(errors))

    # --- 대기열 ---
    #   scores.db는 osu!가 종료할 때 덮어쓰므로 osu!가 켜져 있는 동안에는 고칠 수 없다.
    #   복구, 삭제, 되돌리기는 모두 대기열에 넣고 osu!가 꺼져 있을 때 실행한다.
    #   파일에 저장하므로 툴을 먼저 닫아도 다음 실행 때 이어서 처리한다.
    def load_queue(self):
        try:
            with open(self.queue_path, encoding="utf-8") as f:
                ops = json.load(f)
            return ops if isinstance(ops, list) else []
        except (OSError, ValueError):
            return []

    def save_queue(self):
        tmp = self.queue_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.queue, f, ensure_ascii=False)
        os.replace(tmp, self.queue_path)
        self.queue_rev += 1

    def enqueue(self, kind, count, **fields):
        op = {"id": secrets.token_hex(6), "kind": kind, "count": count, "osu_dir": self.osu_dir, **fields}
        with self.lock:
            self.queue.append(op)
            self.save_queue()
        self.kick()

    def kick(self):
        """osu!가 꺼져 있고 다른 작업이 없으면 대기열을 처리한다."""
        with self.lock:
            if not self.queue or self.job:
                return False
            osu_dir = self.queue[0]["osu_dir"]
        if osu_pids(osu_dir):
            self.last_running = time.time()
            return False
        if time.time() - self.last_running < OSU_EXIT_GRACE_SEC:
            return False
        return self.start_job("queue", self.run_queue)

    def run_queue(self):
        msgs, errors, rescan = [], [], False
        while True:
            with self.lock:
                op = self.queue[0] if self.queue else None
            if not op or osu_pids(op["osu_dir"]):
                break
            try:
                msgs.append({"recover": self.apply_recover, "delete": self.apply_delete,
                             "undo": self.apply_undo}[op["kind"]](op))
                rescan = rescan or op["kind"] == "undo"
            except OsuRunning:
                self.last_running = time.time()
                break
            except RecoveryError as e:
                errors.append(str(e))
            with self.lock:
                self.queue = [o for o in self.queue if o["id"] != op["id"]]
                self.save_queue()
        if errors and not msgs:
            raise RecoveryError(" ".join(errors))
        return {"msg": " ".join(msgs + errors), "rescan": rescan}

    def public_queue(self):
        with self.lock:
            return [{"id": op["id"], "kind": op["kind"], "count": op["count"],
                     "keys": [f"{md5}:{ts}" for md5, ts in op.get("keys", [])],
                     "history": op.get("history")} for op in self.queue]

    def queue_remove(self, key=None):
        """대기 중인 기록 하나를 빼거나, key가 없으면 전부 취소한다."""
        with self.lock:
            if key is None:
                self.queue = []
            else:
                kept = []
                for op in self.queue:
                    keys = [k for k in op.get("keys", []) if f"{k[0]}:{k[1]}" != key]
                    if len(keys) != len(op.get("keys", [])):
                        op = {**op, "keys": keys, "count": len(keys)}
                        wanted = {f"{k[0]}:{k[1]}" for k in keys}
                        if "entries" in op:
                            op["entries"] = [e for e in op["entries"] if e["key"] in wanted]
                        if not keys:
                            continue
                    kept.append(op)
                self.queue = kept
            self.save_queue()

    # --- 요청 ---
    def request_recover(self, ids):
        self.require_osu_dir()
        rows = [self.cands[i] for i in ids if i in self.cands]
        if not rows:
            raise RecoveryError(tr("srv.nothingRecover"))
        keys = [[s["md5"], str(s["ts"])] for s in rows]
        entries = [{"key": f"{s['md5']}:{s['ts']}", "raw": base64.b64encode(s["raw"]).decode("ascii")} for s in rows]
        self.enqueue("recover", len(rows), keys=keys, entries=entries)

    def request_delete(self, keys):
        self.require_osu_dir()
        clean = []
        for pair in keys:
            if (isinstance(pair, list) and len(pair) == 2 and MD5_RE.fullmatch(str(pair[0]))
                    and str(pair[1]).isdigit()):
                clean.append([str(pair[0]), str(pair[1])])
        if not clean:
            raise RecoveryError(tr("srv.nothingDelete"))
        wanted = {(md5, int(ts)) for md5, ts in clean}
        paths = {f"{s['md5']}:{s['ts']}": s.get("path") for s in self.cands.values()
                 if (s["md5"], s["ts"]) in wanted and s.get("path")}
        self.enqueue("delete", len(clean), keys=clean, paths=paths)

    def request_undo(self, stamp):
        if not re.fullmatch(r"[0-9_]+", stamp or ""):
            raise RecoveryError(tr("srv.badRequest"))
        _, meta = self.read_meta(stamp)
        if meta.get("undone"):
            raise RecoveryError(tr("srv.alreadyUndone"))
        with self.lock:
            if any(op.get("history") == stamp for op in self.queue):
                return
            self.queue.append({"id": secrets.token_hex(6), "kind": "undo", "count": meta["count"],
                               "osu_dir": meta["osu_dir"], "history": stamp})
            self.save_queue()
        self.kick()

    # --- 복구 ---
    def apply_recover(self, op):
        osu_dir = op["osu_dir"]
        self.set_progress(msg=tr("srv.recovering"))
        scores_path = os.path.join(osu_dir, "scores.db")
        # osu!가 종료하면서 직접 저장한 기록이 있을 수 있어 파일을 다시 읽는다 (중복 방지)
        version, beatmaps = read_scores_db(scores_path)
        have = {(s["md5"], s["ts"]) for _, _, ss in beatmaps for s in ss}
        rows = [parse_score_entry(base64.b64decode(e["raw"])) for e in op["entries"]]
        todo = [s for s in rows if (s["md5"], s["ts"]) not in have]
        map_ok = True
        if todo:
            if osu_pids(osu_dir):
                raise OsuRunning()
            self.new_history(osu_dir, scores_path, "recover", len(todo),
                             keys=[[s["md5"], s["ts"]] for s in todo])
            add_scores(beatmaps, todo)
            if version is None:
                version = max(s["version"] for s in todo)
            write_scores_db(scores_path, version, beatmaps)
            map_ok = update_osu_db(osu_dir, beatmaps, {(s["md5"], s["mode"]) for s in todo})
        self.forget_candidates({(s["md5"], s["ts"]) for s in rows})
        skipped = len(rows) - len(todo)
        msg = tr("srv.recovered", n=len(todo))
        if skipped:
            msg += tr("srv.recoverSkipped", n=skipped)
        if not map_ok:
            msg += tr("srv.mapInfoFailed")
        return msg

    def forget_candidates(self, keys):
        with self.lock:
            self.cands = {i: s for i, s in self.cands.items() if (s["md5"], s["ts"]) not in keys}

    # --- 삭제: 리플레이도 보관 폴더로 옮긴다. 남겨 두면 복구 후보로 다시 나타난다. ---
    def replay_files(self, osu_dir, md5, ts, known_path=None):
        stem = f"{md5}-{ts - FILETIME_OFFSET}"
        found = []
        for folder in self.replay_dirs(osu_dir):
            for ext in (".osr", ".osg"):
                path = os.path.join(folder, stem + ext)
                if os.path.isfile(path):
                    found.append(path)
        if known_path and os.path.isfile(known_path) and not any(same_dir(known_path, f) for f in found):
            found.append(known_path)
        return found

    def apply_delete(self, op):
        osu_dir = op["osu_dir"]
        self.set_progress(msg=tr("srv.deleting"))
        scores_path = os.path.join(osu_dir, "scores.db")
        version, beatmaps = read_scores_db(scores_path)
        wanted = {(md5, int(ts)) for md5, ts in op["keys"]}
        raws, kept = {}, []
        for md5, raw, ss in beatmaps:
            left = []
            for score in ss:
                key = (score["md5"], score["ts"])
                if key in wanted:
                    raws[key] = score["raw"]
                else:
                    left.append(score)
            if left:
                kept.append((md5, raw, left))
        plan = {key: self.replay_files(osu_dir, key[0], key[1], op.get("paths", {}).get(f"{key[0]}:{key[1]}"))
                for key in wanted}
        targets = [k for k in wanted if k in raws or plan[k]]
        if not targets:
            return tr("srv.deleteNotFound")
        if osu_pids(osu_dir):
            raise OsuRunning()

        items = [{"md5": k[0], "ts": k[1], "raw": base64.b64encode(raws[k]).decode("ascii") if k in raws else None,
                  "files": []} for k in targets]
        folder, meta_path, meta = self.new_history(osu_dir, scores_path, "delete", len(targets), items=items)
        trash = os.path.join(self.trash_root(osu_dir), meta["id"])
        map_ok = True
        if raws:
            write_scores_db(scores_path, version, kept)
            map_ok = self.update_after_delete(osu_dir, raws, kept)
        failed = 0
        for item in items:
            for path in plan[(item["md5"], item["ts"])]:
                dest = os.path.join(trash, os.path.basename(path))
                try:
                    os.makedirs(trash, exist_ok=True)
                    shutil.move(path, dest)
                    item["files"].append([path, dest])
                except OSError:
                    failed += 1
        write_json(meta_path, meta)
        self.forget_candidates(set(targets))
        msg = tr("srv.deleted", n=len(targets))
        if failed:
            msg += tr("srv.moveFailed", n=failed)
        if not map_ok:
            msg += tr("srv.mapInfoFailed")
        return msg

    @staticmethod
    def update_after_delete(osu_dir, removed, kept):
        """등급을 남은 기록에 맞춘다. 맵의 가장 최근 기록을 지웠다면 마지막 플레이도
        남은 기록 중 가장 최근 시각으로 내린다."""
        gone, left = {}, {}
        for md5, ts in removed:
            gone[md5] = max(gone.get(md5, 0), ts)
        for _md5, _raw, ss in kept:
            for s in ss:
                left[s["md5"]] = max(left.get(s["md5"], 0), s["ts"])

        def decide(md5, last):
            if md5 not in gone or gone[md5] < left.get(md5, 0):
                return None
            new = left.get(md5, 0)
            return new if new < last else None
        changed = {(md5, parse_score_entry(raw)["mode"]) for (md5, _ts), raw in removed.items()}
        return update_osu_db(osu_dir, kept, changed, decide)

    # --- 내역 ---
    def new_history(self, osu_dir, scores_path, kind, count, **fields):
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        d = os.path.join(self.history_root, stamp)
        os.makedirs(d)
        if os.path.isfile(scores_path):
            shutil.copy2(scores_path, os.path.join(d, "scores.db"))
        meta = {"id": stamp, "kind": kind, "time": int(time.time() * 1000), "osu_dir": osu_dir,
                "count": count, "undone": False, **fields}
        meta_path = os.path.join(d, "meta.json")
        write_json(meta_path, meta)
        self.prune_history()
        return d, meta_path, meta

    def prune_history(self):
        """복구 내역과 scores.db 사본은 오래되면 지운다. 삭제 내역은 복원에 필요하므로 남긴다."""
        for old in sorted(os.listdir(self.history_root))[:-KEEP_HISTORY]:
            folder = os.path.join(self.history_root, old)
            try:
                _, meta = self.read_meta(old)
            except (OSError, ValueError):
                continue
            if meta.get("kind") == "delete" and not meta.get("undone"):
                try:
                    os.remove(os.path.join(folder, "scores.db"))
                except OSError:
                    pass
            else:
                shutil.rmtree(folder, ignore_errors=True)

    def read_meta(self, stamp):
        path = os.path.join(self.history_root, stamp, "meta.json")
        with open(path, encoding="utf-8") as f:
            return path, json.load(f)

    def public_history(self):
        out = []
        for stamp in sorted(os.listdir(self.history_root), reverse=True):
            try:
                _, meta = self.read_meta(stamp)
            except (OSError, ValueError):
                continue
            meta.pop("keys", None)
            meta.pop("items", None)
            meta.setdefault("kind", "recover")
            out.append(meta)
        return out

    # --- 되돌리기: 그 작업에서 다룬 기록만 되돌린다. 그 뒤에 친 기록은 그대로 남는다. ---
    def apply_undo(self, op):
        meta_path, meta = self.read_meta(op["history"])
        if meta.get("undone"):
            return tr("srv.alreadyUndone")
        osu_dir = meta["osu_dir"]
        self.set_progress(msg=tr("srv.undoing"))
        scores_path = os.path.join(osu_dir, "scores.db")
        version, beatmaps = read_scores_db(scores_path)
        if osu_pids(osu_dir):
            raise OsuRunning()
        if meta.get("kind") == "delete":
            msg = self.undo_delete(meta, scores_path, version, beatmaps)
        else:
            msg = self.undo_recover(meta, scores_path, version, beatmaps)
        meta["undone"] = True
        write_json(meta_path, meta)
        return msg

    @staticmethod
    def undo_recover(meta, scores_path, version, beatmaps):
        if version is None:
            raise RecoveryError(tr("srv.noDb"))
        keys = {(md5, ts) for md5, ts in meta["keys"]}
        removed, kept, changed = 0, [], set()
        for md5, raw, ss in beatmaps:
            left = []
            for s in ss:
                if (s["md5"], s["ts"]) in keys:
                    removed += 1
                    changed.add((s["md5"], parse_score_entry(s["raw"])["mode"]))
                else:
                    left.append(s)
            if left:
                kept.append((md5, raw, left))
        write_scores_db(scores_path, version, kept)
        msg = tr("srv.unrecovered", n=removed)
        if changed and not update_osu_db(meta["osu_dir"], kept, changed):
            msg += tr("srv.mapInfoFailed")
        return msg

    @staticmethod
    def undo_delete(meta, scores_path, version, beatmaps):
        have = {(s["md5"], s["ts"]) for _, _, ss in beatmaps for s in ss}
        rows = [parse_score_entry(base64.b64decode(i["raw"])) for i in meta["items"] if i["raw"]]
        todo = [s for s in rows if (s["md5"], s["ts"]) not in have]
        if todo:
            add_scores(beatmaps, todo)
            if version is None:
                version = max(s["version"] for s in todo)
            write_scores_db(scores_path, version, beatmaps)
        # 돌아온 기록이 더 최근일 때만 올린다. 삭제 뒤에 다시 플레이한 시각은 덮어쓰지 않는다.
        back = {}
        for s in todo:
            back[s["md5"]] = max(back.get(s["md5"], 0), s["ts"])
        map_ok = not todo or update_osu_db(meta["osu_dir"], beatmaps, {(s["md5"], s["mode"]) for s in todo},
                                           lambda md5, last: back[md5] if back.get(md5, 0) > last else None)
        missing = 0
        folders = set()
        for item in meta["items"]:
            for orig, stored in item["files"]:
                folders.add(os.path.dirname(stored))
                try:
                    if os.path.exists(stored) and not os.path.exists(orig):
                        os.makedirs(os.path.dirname(orig), exist_ok=True)
                        shutil.move(stored, orig)
                    elif not os.path.exists(orig):
                        missing += 1
                except OSError:
                    missing += 1
        for folder in folders:
            try:
                os.rmdir(folder)
            except OSError:
                pass
        msg = tr("srv.restored", n=meta["count"])
        if missing:
            msg += tr("srv.replayMissing", n=missing)
        if not map_ok:
            msg += tr("srv.mapInfoFailed")
        return msg


# ---------------------------------------------------------------------------
# 로컬 서버
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "REosu"

    def log_message(self, *args):
        pass

    @property
    def app(self):
        return self.server.app

    def allowed(self):
        # 다른 사이트가 로컬 서버를 호출하지 못하도록 Host와 토큰을 확인한다.
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
            return False
        origin = self.headers.get("Origin")
        if origin and origin != f"http://127.0.0.1:{self.server.server_port}":
            return False
        return True

    def has_token(self, query=None):
        token = (query or {}).get("t", [None])[0] or self.headers.get("X-Token")
        return bool(token) and secrets.compare_digest(token, self.server.token)

    def send_bytes(self, data, ctype="application/json; charset=utf-8", status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, status=200):
        self.send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"), status=status)

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not self.allowed():
            return self.send_json({"error": "forbidden"}, 403)
        if url.path == "/":
            if not self.has_token(query):
                return self.send_json({"error": "forbidden"}, 403)
            with open(os.path.join(resource_dir(), "web", "index.html"), "rb") as f:
                return self.send_bytes(f.read(), "text/html; charset=utf-8")
        if url.path.startswith("/fonts/"):
            name = url.path[len("/fonts/"):]
            if name not in FONT_FILES:
                return self.send_json({"error": "not found"}, 404)
            with open(os.path.join(resource_dir(), "web", "fonts", name), "rb") as f:
                return self.send_bytes(f.read(), "font/woff2")
        if url.path == "/i18n.json":
            with open(os.path.join(resource_dir(), "web", "i18n.json"), "rb") as f:
                return self.send_bytes(f.read())
        if url.path == "/api/events":
            if not self.has_token(query):
                return self.send_json({"error": "forbidden"}, 403)
            return self.stream_events()
        if not self.has_token():
            return self.send_json({"error": "forbidden"}, 403)
        if url.path == "/api/config":
            return self.send_json(self.app.config_view())
        if url.path == "/api/candidates":
            since = query.get("since", ["0"])[0]
            return self.send_json(self.app.public_candidates(int(since) if since.isdigit() else 0))
        if url.path == "/api/saved":
            try:
                return self.send_json(self.app.public_saved())
            except RecoveryError as e:
                return self.send_json({"error": str(e)}, 409)
        if url.path == "/api/queue":
            return self.send_json(self.app.public_queue())
        if url.path == "/api/history":
            return self.send_json(self.app.public_history())
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        if not self.allowed() or not self.has_token():
            return self.send_json({"error": "forbidden"}, 403)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self.send_json({"error": "too large"}, 413)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.send_json({"error": "bad json"}, 400)
        app = self.app
        path = urlparse(self.path).path

        if path == "/api/config":
            app.set_config(body)
            return self.send_json(app.config_view())
        if path == "/api/pick":
            target = body.get("target")
            with app.pick_lock:
                if target == "osu":
                    picked = pick_folder(app.osu_dir, tr("srv.pickOsu"))
                    if picked:
                        app.set_config({"osu_dir": picked})
                elif target == "replay":
                    picked = pick_folder(app.effective_replay_dir(), tr("srv.pickReplay"))
                    if picked:
                        app.set_config({"replay_dir": picked})
                elif target == "backup":
                    picked = pick_folder(app.backup_dir, tr("srv.pickBackup"))
                    if picked:
                        app.set_config({"backup_dir": picked})
                elif target == "trash":
                    picked = pick_folder(app.trash_root(app.osu_dir), tr("srv.pickTrash"))
                    if picked:
                        app.set_config({"trash_dir": picked})
                else:
                    return self.send_json({"error": "bad target"}, 400)
            return self.send_json({"picked": picked, "config": app.config_view()})
        if path == "/api/scan":
            return self.send_json({"started": app.start_job("scan", app.job_scan)})
        if path == "/api/lookup":
            return self.send_json(app.lookup_map(body.get("md5")))
        if path == "/api/open_map":
            set_id, map_id = body.get("set_id"), body.get("map_id")
            if isinstance(set_id, int) and isinstance(map_id, int) and set_id > 0:
                webbrowser.open(f"https://osu.ppy.sh/beatmapsets/{set_id}#/{map_id}")
            return self.send_json({"ok": True})
        if path == "/api/stop":
            app.cancel = True
            return self.send_json({"ok": True})
        if path == "/api/backup/sizes":
            app.measure_sizes()
            return self.send_json({"ok": True})
        if path == "/api/backup":
            keys = [k for k in body.get("keys", []) if k in BACKUP_KEYS]
            return self.send_json({"started": app.start_job("backup", lambda: app.job_backup(keys))})
        if path == "/api/backup/open":
            if app.last_backup and os.path.isdir(app.last_backup):
                os.startfile(app.last_backup)
            return self.send_json({"ok": True})
        try:
            if path == "/api/recover":
                app.request_recover([i for i in body.get("ids", []) if isinstance(i, int)])
                return self.send_json({"ok": True})
            if path == "/api/delete":
                app.request_delete(body.get("keys", []))
                return self.send_json({"ok": True})
            if path == "/api/undo":
                app.request_undo(str(body.get("id")))
                return self.send_json({"ok": True})
        except RecoveryError as e:
            return self.send_json({"error": str(e)})
        except (OSError, ValueError):
            return self.send_json({"error": tr("srv.requestFailed")})
        if path == "/api/open_author":
            webbrowser.open(AUTHOR_URL)
            return self.send_json({"ok": True})
        if path == "/api/queue/remove":
            key = body.get("key")
            app.queue_remove(str(key) if key else None)
            return self.send_json({"ok": True})
        self.send_json({"error": "not found"}, 404)

    def stream_events(self):
        app = self.app
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        with app.lock:
            app.clients += 1
            app.ever_connected = True
        try:
            prev, sent_at = None, 0.0
            while True:
                now = time.time()
                text = json.dumps(app.snapshot(), ensure_ascii=False)
                if text != prev or now - sent_at > 2:
                    self.wfile.write(f"data: {text}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    prev, sent_at = text, now
                time.sleep(0.2)
        except OSError:
            pass
        finally:
            with app.lock:
                app.clients -= 1
                app.last_seen = time.time()


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


def monitor(server):
    """osu! 실행 상태를 갱신하고, 창이 닫히면 프로그램을 끝낸다."""
    app = server.app
    started = time.time()
    while True:
        try:
            running = bool(app.osu_dir and osu_pids(app.osu_dir))
        except OSError:
            running = False
        if running:
            app.last_running = time.time()
        try:
            app.kick()
        except OSError:
            pass
        with app.lock:
            app.osu_running = running
            closed = app.ever_connected and app.clients == 0 and time.time() - app.last_seen > 6
            if closed and app.job and app.job["kind"] in ("scan", "backup"):
                app.cancel = True
            gone = closed and app.job is None
            never = not app.ever_connected and time.time() - started > 120
        if server.auto_exit and (gone or never):
            server.done.set()
            return
        time.sleep(2)


def show_app_window(url):
    """자체 WebView2 창을 띄우고 닫힐 때까지 기다린다. 런타임이 없어 띄우지 못하면 False."""
    try:
        import webview
        webview.create_window(APP_NAME, url, width=1200, height=880, min_size=(760, 560),
                              background_color="#0a0d1a")
        webview.start(gui="edgechromium")  # 옛 IE 엔진으로 넘어가지 않도록 고정한다
        return True
    except Exception:
        return False


def serve(app, port=0, token=None, show_window=True, exit_when_closed=True):
    server = Server(("127.0.0.1", port), Handler)
    server.app = app
    server.token = token or secrets.token_urlsafe(24)
    server.auto_exit = False
    server.done = threading.Event()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=monitor, args=(server,), daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/?t={server.token}"

    if show_window:
        # 예전 버전이 Edge 앱 창에 쓰던 프로필. Edge가 자동 로그인한 흔적이 남아 있을 수 있어 지운다.
        shutil.rmtree(os.path.join(app.data_dir, "window"), ignore_errors=True)
        if show_app_window(url):
            app.cancel = True
            while app.job:  # 복구 중이면 쓰기가 끝날 때까지 기다린다
                time.sleep(0.2)
            return
        webbrowser.open(url)  # WebView2 런타임이 없는 PC: 기본 브라우저로 연다
    server.auto_exit = exit_when_closed
    server.done.wait()


if __name__ == "__main__":
    serve(App())
