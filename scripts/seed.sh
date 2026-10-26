#!/bin/sh
# Prints a random 32-byte hex hash used to seed the shelf's design.
head -c 32 /dev/urandom | shasum -a 256 | cut -d' ' -f1
