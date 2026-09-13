"""cortex — THE BRAIN: the body's state, the /api door, and the picture.

The cortex is the only writer of the store and the only publisher of the
topics. It owns the PICTURE: the face's pages (`face`), the ring's builder
(`ring`) and the assembly that publishes them (`frames`).

  state        RAM state + settings master (was loa/cortex.py)
  store        postgres on aleph
  frames       assembles the frames it publishes
  face         the pages: state -> 1024 bytes
  ring         the ring's frame builder: state -> 72 bytes
  amiga topaz  the face's art and font
  moods        the feelings vocabulary
  expressions  what the face says
  __main__     the /api door, the tick, ingest  (python -m loa.cortex)
"""
