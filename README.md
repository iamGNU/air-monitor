# Air monitor

Watch the 2440 MHz LoRa link of the swarm mesh radio over the air with a HackRF Pro, in a desktop window.

It shows, live:

- the spectrum and waterfall around 2440 MHz (the LoRa channel is shaded),
- the channel power over the last 2 s (every packet is a burst, so TDMA slots line up),
- every burst it hears, decoded: payload text and hex, CRC, level, SNR, frequency error, duration and the
  time since the previous packet.

Radio settings match the v0.1 nodes: 2440.0 MHz, LoRa SF7, BW 1625 kHz, CR 4/5, sync word 0x12.

## Setup

Needs Python 3, `hackrf_transfer` (from the `hackrf` package) and:

```bash
pip install -r requirements.txt
```

## Run

```bash
python3 tools/air_monitor.py                      # live from the HackRF
python3 tools/air_monitor.py --lna 16 --vga 20    # start with other gains
python3 tools/air_monitor.py --file CAP.iq        # replay a capture made with the Record button
```

The HackRF only receives. Keep it at least 1 m from a node that is transmitting, RF amp off, and close Gqrx
first (only one program can use the HackRF at a time). Packets are logged to `logs/air_<time>.csv`.

## Files

- `tools/air_monitor.py`: the monitor window.
- `tools/lora_phy.py`: LoRa encode/decode in plain Python, used to decode the packets.
