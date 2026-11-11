#!/bin/sh
# Guarded headring device shutdown helper. Runs as root via a narrow sudo rule;
# the collection user cannot pass arguments, so the policy stays fixed.
exec /sbin/shutdown -h now
