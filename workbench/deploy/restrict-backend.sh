#!/bin/sh
set -eu
# Docker-published ports bypass ordinary INPUT rules. Restrict the new VM's
# backend port in DOCKER-USER; preserve every unrelated chain and rule.
iptables -N DOCKER-USER 2>/dev/null || true
iptables -N UL-BACKEND-INGRESS 2>/dev/null || true
iptables -F UL-BACKEND-INGRESS
iptables -A UL-BACKEND-INGRESS -s 192.168.100.30/32 -j RETURN
iptables -A UL-BACKEND-INGRESS -j DROP
iptables -C DOCKER-USER -i eth0 -p tcp --dport 5191 -j UL-BACKEND-INGRESS 2>/dev/null || \
    iptables -I DOCKER-USER 1 -i eth0 -p tcp --dport 5191 -j UL-BACKEND-INGRESS
