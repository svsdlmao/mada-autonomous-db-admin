#!/usr/bin/env bash
# Starts the MADA dashboard against a local PostgreSQL 16 (see README section 2).
cd "$(dirname "$0")"
export PGTZ=UTC
python3 -m demo.server
