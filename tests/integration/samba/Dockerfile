# Samba server for Degreaser integration tests and benchmarks.
# Includes iproute2 so latency can be injected with `tc netem` (needs NET_ADMIN),
# and python3 so fixture trees can be generated inside the container's own volume.
FROM alpine:3.22
RUN apk add --no-cache samba samba-common-tools iproute2 python3 attr
COPY smb.conf /etc/samba/smb.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 445
ENTRYPOINT ["/entrypoint.sh"]
