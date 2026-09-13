#!/bin/sh
set -eu
# Only dedicated CI subnets enter these chains. Never flush host/Docker chains.
iptables -w -nL CI5900-FORWARD >/dev/null 2>&1 || iptables -w -N CI5900-FORWARD
iptables -w -nL CI5900-INPUT >/dev/null 2>&1 || iptables -w -N CI5900-INPUT
add() { iptables -w -C "$@" 2>/dev/null || iptables -w -A "$@"; }
add CI5900-FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
for destination in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.168.0.0/16 224.0.0.0/4 240.0.0.0/4; do
    add CI5900-FORWARD -d "$destination" -j REJECT
done
add CI5900-INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
add CI5900-INPUT -j REJECT
for subnet in 10.89.0.0/24 10.90.0.0/24; do
    iptables -w -C DOCKER-USER -s "$subnet" -j CI5900-FORWARD 2>/dev/null || iptables -w -I DOCKER-USER 1 -s "$subnet" -j CI5900-FORWARD
    iptables -w -C INPUT -s "$subnet" -j CI5900-INPUT 2>/dev/null || iptables -w -I INPUT 1 -s "$subnet" -j CI5900-INPUT
done
