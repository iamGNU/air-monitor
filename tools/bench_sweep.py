"""Bench sweep: listen at a ladder of HackRF gains and print how the link does at each, to choose the
Low / Normal / High sensitivity presets and the overload limit (link_health.py).

    python3 tools/bench_sweep.py                         # LNA 0-32 x VGA 10/20/30, 5 s each (~1.5 min)
    python3 tools/bench_sweep.py --seconds 8 --lna 8,16 --vga 10,20 --csv sweep_1m.csv
    python3 tools/bench_sweep.py --file CAP.iq           # one row from a recording (to try the tool)

Run it with a node transmitting, at a few distances (e.g. 0.5 m, 1 m, 3 m), and keep the tables. Close Air Monitor
and Gqrx first: only one program can use the HackRF. The HackRF only receives (RF amp off).
"""
import argparse
import csv
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import air_monitor as am  # noqa: E402
import link_health as lh  # noqa: E402
import payloads  # noqa: E402
import radio_config  # noqa: E402

WARMUP_S = 0.5              # packets in the first half second are ignored (the HackRF settling after a start)
FIELDS = ["lna", "vga", "read", "damaged", "not_heard", "lost_pct", "peak_dbfs", "median_dbfs", "median_snr_db",
          "other"]


def measure(chunks, warmup_s=WARMUP_S):
    """Run the receiver over an iterable of raw int8 IQ chunks (2 * BLOCK bytes each); returns the row values."""
    rx = am.Receiver()
    seqs = payloads.SequenceTracker()
    ok = bad = other = 0
    levels, snrs = [], []
    for raw in chunks:
        for pk in rx.process(raw)["packets"]:
            if pk["stream_s"] < warmup_s:
                continue
            if pk["kind"] == "other":
                other += 1
                continue
            levels.append(float(pk["level_db"]))
            snrs.append(float(pk["snr_db"]))
            if pk["kind"] == "ok":
                ok += 1
                d = payloads.describe(pk["data"])
                if d.seq is not None:
                    seqs.good(d.stream, d.seq)
            else:
                bad += 1
                seqs.bad()
    lost = bad + seqs.not_heard
    return dict(read=ok, damaged=bad, not_heard=seqs.not_heard,
                lost_pct=round(100 * lost / (ok + lost), 1) if ok + lost else None,
                peak_dbfs=round(max(levels), 1) if levels else None,
                median_dbfs=round(statistics.median(levels), 1) if levels else None,
                median_snr_db=round(statistics.median(snrs), 1) if snrs else None, other=other)


def file_chunks(path):
    with open(path, "rb") as f:
        while len(raw := f.read(2 * am.BLOCK)) == 2 * am.BLOCK:
            yield raw


def hackrf_chunks(lna, vga, seconds):
    n = int(seconds * am.FS) // am.BLOCK * am.BLOCK
    proc, src = am.open_hackrf(lna, vga, ["-n", str(n)])        # -n: it stops by itself after n samples
    try:
        while (raw := am._read_exact(src, 2 * am.BLOCK)) is not None:
            yield raw
    finally:
        am.stop_hackrf(proc, src)
        err = proc.stderr.read().decode(errors="replace")
        if proc.returncode != 0 and "hackrf_open" in err:
            raise SystemExit("HackRF: " + lh.friendly_error(err)[0] + "\n" + err.strip())


def show(row):
    cells = [f"{row['lna']:>3}", f"{row['vga']:>3}"] + [
        f"{'–' if row[k] is None else row[k]:>{w}}" for k, w in
        [("read", 5), ("damaged", 8), ("not_heard", 10), ("lost_pct", 7), ("peak_dbfs", 10), ("median_dbfs", 12),
         ("median_snr_db", 8), ("other", 6)]]
    over = row["peak_dbfs"] is not None and row["peak_dbfs"] > lh.CLIP_DB
    print("  ".join(cells) + ("   too strong" if over else ""), flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=5.0, help="listening time per setting (default 5)")
    ap.add_argument("--lna", default="0,8,16,24,32", help="LNA gains to try, dB (multiples of 8)")
    ap.add_argument("--vga", default="10,20,30", help="VGA gains to try, dB (even, 0-62)")
    ap.add_argument("--file", help="measure a recording once in place of the HackRF")
    ap.add_argument("--config", help="radio settings file (default: air-monitor.toml, if it exists)")
    ap.add_argument("--csv", help="also save the table to this CSV file")
    args = ap.parse_args()
    try:
        am.configure(radio_config.load(args.config))
    except radio_config.ConfigError as e:
        sys.exit(str(e))

    print(f"Radio: {am.RADIO.label()}.  'lost' = damaged + not heard (from the packet counters); "
          f"'too strong' = peak above {lh.CLIP_DB:g} dBFS.\n")
    print("LNA  VGA   read  damaged  not heard  lost %  peak dBFS  median dBFS  SNR dB  other")
    rows = []
    settings = [("-", "-")] if args.file else \
        [(int(lna), int(vga)) for lna in args.lna.split(",") for vga in args.vga.split(",")]
    for lna, vga in settings:
        if args.file:
            row = dict(lna=lna, vga=vga, **measure(file_chunks(args.file), warmup_s=0))
        else:
            row = dict(lna=lna, vga=vga, **measure(hackrf_chunks(lna, vga, args.seconds + WARMUP_S)))
        rows.append(row)
        show(row)

    trials = {f"LNA {r['lna']}, VGA {r['vga']}": dict(ok=r["read"], bad=r["damaged"], not_heard=r["not_heard"],
                                                      peak=r["peak_dbfs"], snr=r["median_snr_db"]) for r in rows}
    best, _ = lh.pick_sensitivity(trials)
    if not best:
        print("\nNo packets heard. Is a node switched on and transmitting?")
    elif not args.file:
        print(f"\nBest here: {best}")
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, FIELDS)
            w.writeheader()
            w.writerows(rows)
        print(f"Saved {args.csv}")


if __name__ == "__main__":
    main()
