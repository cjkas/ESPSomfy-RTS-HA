What a real device sent: `hello.json`, `sync.json` (the frames of one sync) and `schema.json`. They are copies of the firmware repository's `test/fixtures/proto2`, which its e2e suite records and scrubs (`pytest test/e2e --record`): names, addresses and rolling codes are made up.

Copy them again when the firmware's schema changes.
