# Air Monitor

See what is happening on the 2440 MHz LoRa link of the swarm mesh radio, live, with a HackRF Pro.

![Air Monitor showing a working link](docs/air-monitor.png)

## Quick start

1. Install the HackRF software: `sudo apt install hackrf`
2. Plug in the HackRF and run `./air-monitor`
3. Press **Start listening**.

No HackRF? Press **Try the demo**: it plays an example recording, so you can learn the screen first.

The first `./air-monitor` sets up the Python packages it needs (in `.venv`) if they are not installed already.

## Reading the screen

- **Status** (top): whether the link is working, in plain words, and what to do if it isn't. When the signal is
  too strong or too weak, it offers a button that changes the sensitivity for you.
- **Received / Damaged / Signal / Quality**: packets read correctly, packets that failed their check, how loud
  the nodes are at the HackRF, and how clearly they stand out from the noise.
- **What's on the air**: a waterfall of the last 10 s, newest at the top; brighter means stronger. The dashed
  lines mark the LoRa channel, where each packet shows as a bright bar. The thin column beside it has a tick for
  every packet: green = read OK, red = damaged, grey = some other signal (Wi-Fi, Bluetooth…).
- **Packets**: the channel over the last 2 s. Each spike is one transmission; evenly spaced spikes are the nodes
  taking turns.
- **Messages**: what each packet said, newest first.
- **Sensitivity**: Normal suits most cases. Use Low if the status says the signal is too strong, High if it is
  weak.
- **Show details**: the spectrum, the raw LNA/VGA gains, the technical columns (Δt, length, CRC, level, SNR,
  frequency error, duration, hex) and the receiver health.
- **⋯ menu**: play the demo or a recording, record raw radio data, open the logs folder, help.

Hover over anything for an explanation. Space starts and stops listening.

## Good to know

- The HackRF only receives. Keep it at least 1 m from a node that is transmitting (RF amp off).
- Only one program can use the HackRF at a time: close Gqrx first.
- Every packet heard is logged to `logs/air_<time>.csv`. "Record raw radio data" saves the raw signal (about
  13 MB/s) to `logs/capture_<time>…iq`, to play back later with "Play a recording…".

Radio settings match the v0.1 nodes: 2440.0 MHz, LoRa SF7, BW 1625 kHz, CR 4/5, sync word 0x12.

## Command line

```bash
./air-monitor                         # same as: python3 tools/air_monitor.py
./air-monitor --demo                  # the example recording
./air-monitor --file CAP.iq           # play a recording
./air-monitor --lna 16 --vga 20       # start with these gains (default: the last sensitivity used)
./air-monitor --details               # open with the details shown
./air-monitor --autostart             # start listening at once
```

`./air-monitor --help` lists every option. Without the launcher: `pip install -r requirements.txt`, then
`python3 tools/air_monitor.py`.

## Files

- `air-monitor`: the launcher.
- `tools/air_monitor.py`: the receiver (HackRF input, burst detection, LoRa decoding) and the command line.
- `tools/air_monitor_ui.py`: the window.
- `tools/link_health.py`: the plain-language rules: status line, ratings, sensitivity presets and error messages.
  The thresholds to tune on the bench are at the top.
- `tools/demo_capture.py`: makes the demo recording (cached in the temp folder on first use).
- `tools/lora_phy.py`: LoRa encode/decode in plain Python, used to decode the packets.
- `tests/`: run with `python3 -m unittest discover tests`.
