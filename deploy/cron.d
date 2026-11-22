SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Every 5 minutes. flock prevents overlapping runs when a backlog is large.
*/5 * * * * blink /usr/bin/flock -n /var/lib/blink-to-telegram/forwarder.lock /usr/local/bin/blink-forwarder >> /var/log/blink-forwarder.log 2>&1
