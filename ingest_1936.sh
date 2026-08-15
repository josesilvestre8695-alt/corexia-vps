#!/usr/bin/env bash
# Redirect da porta de ingest 1936 -> 1935 (MediaMTX). A 1936 e a porta que as redes
# das cameras deixam sair (a 1935 costuma ser bloqueada). Reaplica no boot.
iptables -t nat -C PREROUTING -p tcp --dport 1936 -j REDIRECT --to-ports 1935 2>/dev/null \
  || iptables -t nat -A PREROUTING -p tcp --dport 1936 -j REDIRECT --to-ports 1935
