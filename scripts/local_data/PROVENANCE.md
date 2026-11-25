# Local data modules

These project modules use only the Python standard library and take explicit input/output paths. They do not upload to a NAS or start a scheduler on import.

The retained baseline SHA-256 values are `7ba722838420e9a8ec728bcb3212b0cf2a2ebfe63dc3caae1179d54302b638bd` for `export_records.py` and `2541143c6f5f6df355d09fd1bf0e82c16d0caca379ebd6879ccbcf2e24f22fbe` for `build_index.py`.

The exporter projects the collector streams to dated JSONL with an `export_info` footer. The indexer consumes those projected files, not raw collector data. A calendar date is not a session boundary; a room tag is not a speaker identity. Masking URL/email/phone patterns does not guarantee anonymity. Synthetic tests need no private data, NAS client, or model.
