#!/usr/bin/env bash
# Regenerate the protobuf bindings from proto/loa.proto.
#
# proto/loa.proto is the source of truth. loa/pb/loa_pb2.py is GENERATED and
# committed (the Pi installs from git and must not need a compiler).
#
# If you changed the SHAPE, bump SCHEMA_VERSION in loa/topic.py in the same
# commit — that constant is what makes a mismatched consumer fail loudly instead
# of decoding plausible rubbish.
set -euo pipefail
cd "$(dirname "$0")/.."
uv run python -m grpc_tools.protoc -Iproto --python_out=loa/pb proto/loa.proto
echo "regenerated loa/pb/loa_pb2.py"
echo "SCHEMA_VERSION in loa/topic.py is $(grep -m1 '^SCHEMA_VERSION' loa/topic.py)"
