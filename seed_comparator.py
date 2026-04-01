#!/usr/bin/env python3
"""
MCSR Seed Comparator
────────────────────
Fetches all recent matches for a player, scans every seed against
the local dataset, and surfaces only the matches where other players
have played the same seed — ranked fastest to slowest.

Requirements:
    pip install requests rich
"""

import glob
import json
import os
import re
import sys
import requests
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Force UTF-8 output on Windows
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt
from rich import box
from rich.rule import Rule
from rich.align import Align

# ── Config ────────────────────────────────────────────────────────────────────
MATCHES_FILE     = "season_10_20260331_000939-compressed.jsonl"
MAX_SEED_RESULTS = 25   # max rows shown per seed leaderboard
API_BASE         = "https://api.mcsrranked.com"
# ─────────────────────────────────────────────────────────────────────────────

console = Console(force_terminal=True)

SEED_TYPE = {
    "S": "SHIPWRECK",      "V": "VILLAGE",
    "R": "RUINED_PORTAL",  "D": "DESERT_TEMPLE",
    "B": "BURIED_TREASURE",
}
BASTION_TYPE = {
    "H": "HOUSING",  "T": "TREASURE",
    "G": "BRIDGE",   "A": "STABLES",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def ms_to_time(ms) -> str:
    if ms is None:
        return "—"
    total_sec = int(ms) // 1000
    m, s = divmod(total_sec, 60)
    return f"{m}:{s:02d}"


def decode_seed_type(code) -> str:
    return SEED_TYPE.get(code, code or "?")


def decode_bastion_type(code) -> str:
    return BASTION_TYPE.get(code, code or "?")

def sort_dataset_paths(paths: list[str]) -> list[str]:
    def sort_key(path: str):
        match = re.search(r"\.part(\d+)\.jsonl$", path, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return path
    return sorted(paths, key=sort_key)


def find_dataset_paths(path: str) -> list[str]:
    if os.path.exists(path):
        return [path]

    dirname = os.path.dirname(path) or "."
    basename = os.path.basename(path)
    candidates = []

    if basename.lower().endswith(".jsonl"):
        stem = basename[:-6]
        candidates.extend(glob.glob(os.path.join(dirname, f"{stem}.part*.jsonl")))
        candidates.extend(glob.glob(os.path.join(dirname, f"{basename}.part*.jsonl")))
    else:
        candidates.extend(glob.glob(os.path.join(dirname, f"{basename}.part*.jsonl")))

    candidates = [c for c in candidates if os.path.isfile(c)]
    if not candidates and basename.lower().endswith(".jsonl"):
        stem = basename[:-6]
        candidates.extend(glob.glob(os.path.join(dirname, f"{stem}.jsonl.*")))

    return sort_dataset_paths(candidates)

# ── Dataset ───────────────────────────────────────────────────────────────────

def load_dataset(path: str) -> list:
    paths = find_dataset_paths(path)
    if not paths:
        console.print(f"[yellow]Warning:[/yellow] {path} not found — seed comparison unavailable.")
        return []

    if len(paths) > 1:
        console.print(f"[cyan]Loading dataset from {len(paths)} sections...[/cyan]")

    dataset = []
    for dataset_path in paths:
        if dataset_path.lower().endswith(".jsonl"):
            with open(dataset_path, "r", encoding="utf-8") as f:
                for lineno, line in enumerate(f, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError as exc:
                        console.print(f"[red]Invalid JSON on line {lineno} in {dataset_path}: {exc}[/red]")
                        continue
                    dataset.append(item)
        else:
            with open(dataset_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                dataset.extend(data)
            else:
                console.print(f"[yellow]Warning:[/yellow] {dataset_path} did not contain a JSON list. No matches loaded.")

    return dataset


def dataset_match_id(m):
    return m[0] if isinstance(m, list) and len(m) > 0 else None


def dataset_seed_id(m):
    return m[1] if isinstance(m, list) and len(m) > 1 else None


def dataset_result(m):
    return m[2] if isinstance(m, list) and len(m) > 2 else [None, None]


def dataset_vods(m):
    return m[3] if isinstance(m, list) and len(m) > 3 else []


def dataset_date(m):
    return m[4] if isinstance(m, list) and len(m) > 4 else None


def dataset_type_pair(m):
    return m[5] if isinstance(m, list) and len(m) > 5 else []


def dataset_players(m):
    return m[6] if isinstance(m, list) and len(m) > 6 else []


def build_seed_index(dataset: list) -> dict:
    index = {}
    for m in dataset:
        seed = dataset_seed_id(m)
        if seed:
            index.setdefault(seed, []).append(m)
    return index


# ── API ───────────────────────────────────────────────────────────────────────

def fetch_player_matches(username: str) -> list:
    try:
        resp = requests.get(
            f"{API_BASE}/users/{username}/matches",
            params={"count": 100, "type": 2},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("data") or []
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        if code == 404:
            console.print(f"[red]Player '{username}' not found.[/red]")
        else:
            console.print(f"[red]API error {code}: {e}[/red]")
        return []
    except Exception as e:
        console.print(f"[red]Request failed: {e}[/red]")
        return []


def cleanup_api_match(m: dict) -> dict | None:
    result = m.get("result") or {}
    seed   = m.get("seed")   or {}
    if not result.get("uuid") or not seed.get("id") or m.get("forfeited"):
        return None
    return {
        "id":          m["id"],
        "seed":        seed["id"],
        "result":      result,
        "vod":         m.get("vod") or [],
        "date":        m.get("date"),
        "seedType":    m.get("seedType"),
        "bastionType": m.get("bastionType"),
        "players":     m.get("players") or [],
    }


# ── Core logic ────────────────────────────────────────────────────────────────

def find_hits(player_matches: list, seed_index: dict) -> list:
    """
    Scan every player match against the seed index.
    Returns [(player_match, [other_dataset_matches])] for seeds that hit,
    excluding the player's own match from the 'others' list if present.
    """
    hits = []
    for pm in player_matches:
        seed = pm.get("seed", "")
        if not seed:
            continue
        dataset_matches = seed_index.get(seed, [])
        if not dataset_matches:
            continue

        player_id = pm["id"]
        others = [
            dm for dm in dataset_matches
            if dataset_match_id(dm) != player_id
        ]

        hits.append((pm, others))

    return hits


# ── Match field accessors (handle compressed + uncompressed) ──────────────────

def winner_uuid(m) -> str:
    if isinstance(m, list):
        result = dataset_result(m)
        return result[0] or ""
    return (m.get("result") or {}).get("uuid", "")


def finish_time(m):
    if isinstance(m, list):
        result = dataset_result(m)
        return result[1]
    return (m.get("result") or {}).get("time")


def match_date(m):
    if isinstance(m, list):
        return dataset_date(m)
    return m.get("date")


def player_names(m: dict) -> tuple[str, str]:
    """(winner_name, loser_name)"""
    if isinstance(m, list):
        w_uuid = winner_uuid(m)
        players = dataset_players(m)
        w = next((p[1] for p in players if isinstance(p, list) and len(p) > 1 and p[0] == w_uuid), "?")
        l = next((p[1] for p in players if isinstance(p, list) and len(p) > 1 and p[0] != w_uuid), "?")
    else:
        w_uuid  = (m.get("result") or {}).get("uuid", "")
        players = m.get("players") or []
        w = next((p["nickname"] for p in players if p.get("uuid") == w_uuid), "?")
        l = next((p["nickname"] for p in players if p.get("uuid") != w_uuid), "?")
    return w, l


def format_vod_timestamp(offset_s: int) -> str:
    if offset_s < 0:
        offset_s = 0
    h, rem = divmod(offset_s, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if h or m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return "".join(parts)


def build_vod_url(url: str, offset_s: int) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["t"] = format_vod_timestamp(offset_s)
    return urlunparse(parsed._replace(query=urlencode(query)))


# ── Rendering ─────────────────────────────────────────────────────────────────

def render_summary_table(hits: list, username: str) -> Table:
    """Overview: one row per hit."""
    table = Table(
        title=(
            f"[bold white]{username}[/bold white]"
            f" — {len(hits)} seed{'s' if len(hits) != 1 else ''} found in dataset"
        ),
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        border_style="bright_black",
        title_style="bold white",
        show_lines=True,
    )
    table.add_column("#",         width=3,  justify="right", style="dim")
    table.add_column("Your time", width=9,  style="bold green")
    table.add_column("W/L",       width=5)
    table.add_column("Seed Type", width=16, style="yellow")
    table.add_column("Bastion",   width=10, style="cyan")
    table.add_column("Date",      width=12, style="bright_black")
    table.add_column("Others",    width=7,  justify="right", style="magenta")

    for i, (pm, others) in enumerate(hits, 1):
        t       = finish_time(pm)
        w_uuid  = winner_uuid(pm)
        players = pm.get("players") or []

        user_entry = next(
            (p for p in players if p.get("nickname", "").lower() == username.lower()),
            None
        )
        won    = (user_entry or {}).get("uuid", "") == w_uuid if user_entry else None
        wl_str = "[green]W[/green]" if won else ("[red]L[/red]" if won is False else "[dim]?[/dim]")

        date_ts  = pm.get("date")
        date_str = datetime.fromtimestamp(date_ts).strftime("%Y-%m-%d") if date_ts else "?"

        others_str = str(len(others)) if others else "[dim]only you[/dim]"

        table.add_row(
            str(i),
            ms_to_time(t),
            wl_str,
            decode_seed_type(pm.get("seedType")),
            decode_bastion_type(pm.get("bastionType")),
            date_str,
            others_str,
        )

    return table


def render_seed_leaderboard(pm: dict, others: list, username: str, index: int) -> Table:
    """Detailed leaderboard for one seed, with the player's own run inserted."""
    seed_id   = pm.get("seed", "?")
    seed_type = decode_seed_type(pm.get("seedType"))
    bastion   = decode_bastion_type(pm.get("bastionType"))

    player_time_ms = finish_time(pm)

    # Sort others by time, cap at MAX_SEED_RESULTS
    sorted_others = sorted(
        others,
        key=lambda m: finish_time(m) or 999_999_999
    )[:MAX_SEED_RESULTS]

    # Merge the player's run in at the correct position
    combined: list[tuple[str, dict]] = []
    inserted = False
    for dm in sorted_others:
        dm_time = finish_time(dm) or 999_999_999
        if not inserted and (player_time_ms or 999_999_999) <= dm_time:
            combined.append(("player", pm))
            inserted = True
        combined.append(("other", dm))
    if not inserted:
        combined.append(("player", pm))

    others_label = f"{len(others)} other run{'s' if len(others) != 1 else ''}"
    table = Table(
        title=(
            f"[dim]{index}.[/dim]  "
            f"[bold cyan]{seed_id}[/bold cyan]  ·  "
            f"{seed_type} / {bastion}  ·  "
            f"[magenta]{others_label}[/magenta]"
        ),
        title_justify="left",
        box=box.SIMPLE_HEAD,
        show_header=True,
        header_style="bold cyan",
        border_style="bright_black",
        title_style="white",
        show_lines=False,
        pad_edge=False,
    )
    table.add_column("Rank",   justify="right", style="dim")
    table.add_column("Time",   style="bold")
    table.add_column("Winner")
    table.add_column("Loser",  style="dim")
    table.add_column("Date",   style="bright_black")
    table.add_column("VOD", style="cyan", no_wrap=True, overflow="ignore")

    for rank, (tag, m) in enumerate(combined, 1):
        is_player = (tag == "player")

        if is_player:
            t       = player_time_ms
            w_name, l_name = _api_player_names(pm)
            date_ts = pm.get("date")
        else:
            t       = finish_time(m)
            w_name, l_name = player_names(m)
            date_ts = match_date(m)

        time_str = ms_to_time(t)
        date_str = datetime.fromtimestamp(date_ts).strftime("%Y-%m-%d") if date_ts else "?"

        link = vod_link(m if not is_player else pm)
        vod_cell = f"[cyan]{link}[/cyan]" if link else "[dim]—[/dim]"

        if is_player:
            table.add_row(
                f"[bold magenta]► {rank}[/bold magenta]",
                f"[bold magenta]{time_str}[/bold magenta]",
                f"[bold magenta]{w_name}[/bold magenta]",
                f"[magenta]{l_name}[/magenta]",
                date_str,
                vod_cell,
            )
        else:
            table.add_row(str(rank), time_str, w_name, l_name, date_str, vod_cell)

    return table



def vod_link(m) -> str | None:
    """
    Return a timestamped Twitch VOD URL for a match, or None if unavailable.

    The offset into the stream is: match_start - stream_start
      match_start = date - (result.time / 1000)   [date is when match ENDED]
      stream_start = vod.startsAt

    For dataset matches the vod list is [[url, startsAt], ...].
    For API matches it is a list of objects with url and startsAt.
    """
    date_ts = match_date(m)
    time_ms = finish_time(m)
    if date_ts is None or time_ms is None:
        return None

    match_start_ts = date_ts - (time_ms / 1000)

    if isinstance(m, list):
        vods = dataset_vods(m)
    else:
        vods = m.get("vod") or []
    if not vods:
        return None

    for v in vods:
        if isinstance(m, list):
            if not isinstance(v, list) or len(v) < 2:
                continue
            url = v[0]
            starts_at = v[1]
        else:
            url = v.get("url", "")
            starts_at = v.get("startsAt")
        if not url or starts_at is None:
            continue
        offset_s = int(match_start_ts - starts_at)
        return build_vod_url(url, offset_s)

    return None

def _api_player_names(pm: dict) -> tuple[str, str]:
    """winner_name, loser_name from a cleaned API match (always long-key format)."""
    w_uuid  = (pm.get("result") or {}).get("uuid", "")
    players = pm.get("players") or []
    w = next((p["nickname"] for p in players if p.get("uuid") == w_uuid), "?")
    l = next((p["nickname"] for p in players if p.get("uuid") != w_uuid), "?")
    return w, l


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    console.print()
    console.print(Align.center(
        Panel(
            "[bold white]MCSR  SEED  COMPARATOR[/bold white]\n"
            "[dim]Automatically finds every seed in your history with dataset matches[/dim]",
            border_style="cyan",
            padding=(1, 6),
        )
    ))
    console.print()

    dataset_path = sys.argv[1] if len(sys.argv) > 1 else MATCHES_FILE
    with console.status("[cyan]Loading dataset …[/cyan]"):
        dataset    = load_dataset(dataset_path)
        seed_index = build_seed_index(dataset)

    if dataset:
        console.print(
            f"[green]✓[/green] Loaded [bold]{len(dataset):,}[/bold] matches  ·  "
            f"[bold]{len(seed_index):,}[/bold] unique seeds indexed\n"
        )

    while True:
        username = Prompt.ask(
            "[cyan]Enter Minecraft username[/cyan]  [dim](or Q to quit)[/dim]"
        ).strip()

        if username.upper() == "Q":
            console.print("[dim]Bye![/dim]")
            break
        if not username:
            continue

        with console.status(f"[cyan]Fetching matches for [bold]{username}[/bold] …[/cyan]"):
            raw            = fetch_player_matches(username)
            player_matches = [m for m in (cleanup_api_match(r) for r in raw) if m]

        if not player_matches:
            console.print("[red]No valid matches returned.[/red]\n")
            continue

        console.print(
            f"[green]✓[/green] Fetched [bold]{len(player_matches)}[/bold] matches  ",
            end="",
        )

        with console.status("[cyan]Scanning seeds …[/cyan]"):
            hits = find_hits(player_matches, seed_index)

        console.print(
            f"→  [bold magenta]{len(hits)}[/bold magenta] "
            f"seed{'s' if len(hits) != 1 else ''} found in dataset\n"
        )

        if not hits:
            console.print(
                "[yellow]None of this player's recent seeds appear in the dataset.[/yellow]\n"
                "[dim]The dataset window may not overlap with their recent matches.[/dim]\n"
            )
            continue

        # Summary table
        console.print(render_summary_table(hits, username))
        console.print()

        # One leaderboard per hit
        for i, (pm, others) in enumerate(hits, 1):
            console.print(render_seed_leaderboard(pm, others, username, i))

        console.print()
        console.print(Rule(style="bright_black"))
        console.print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/dim]")
    except Exception:
        import traceback
        console.print("\n[red]--- UNHANDLED ERROR ---[/red]")
        traceback.print_exc()
        input("\nPress Enter to close...")