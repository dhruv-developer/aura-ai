-- Reserve quota before executing. Idempotent per reservation id.
-- KEYS[1] = used counter (committed + reserved)
-- KEYS[2] = reservations hash: id -> amount
-- KEYS[3] = reservation deadlines zset: id -> deadline_ms
-- ARGV[1] = limit (-1 = unlimited)
-- ARGV[2] = amount to reserve
-- ARGV[3] = reservation id
-- ARGV[4] = reservation lease ms (expired reservations are reclaimed)
-- ARGV[5] = counter period expiry ms (applied when the counter is first created)
-- ARGV[6] = now ms
-- Returns {ok(0|1), used_after}
local limit = tonumber(ARGV[1])
local amount = tonumber(ARGV[2])
local rid = ARGV[3]
local lease = tonumber(ARGV[4])
local period = tonumber(ARGV[5])
local now = tonumber(ARGV[6])

-- Reclaim reservations whose holders died without commit/release (bounded work).
local expired = redis.call('ZRANGEBYSCORE', KEYS[3], '-inf', now, 'LIMIT', 0, 100)
for _, old in ipairs(expired) do
  local amt = tonumber(redis.call('HGET', KEYS[2], old))
  if amt then redis.call('DECRBY', KEYS[1], amt) end
  redis.call('HDEL', KEYS[2], old)
  redis.call('ZREM', KEYS[3], old)
end

local used = tonumber(redis.call('GET', KEYS[1])) or 0
if redis.call('HEXISTS', KEYS[2], rid) == 1 then
  return {1, used}
end
if limit >= 0 and used + amount > limit then
  return {0, used}
end

local created = redis.call('EXISTS', KEYS[1]) == 0
used = redis.call('INCRBY', KEYS[1], amount)
if created and period > 0 then redis.call('PEXPIRE', KEYS[1], period) end
redis.call('HSET', KEYS[2], rid, amount)
redis.call('ZADD', KEYS[3], now + lease, rid)
return {1, used}
