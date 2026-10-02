-- Commit a reservation with the actual amount consumed (may differ from the estimate).
-- KEYS as quota_reserve. ARGV[1] = reservation id, ARGV[2] = actual amount
-- Returns {ok(0|1), used_after}. ok=0 means the reservation was unknown/expired.
local rid = ARGV[1]
local actual = tonumber(ARGV[2])
local reserved = tonumber(redis.call('HGET', KEYS[2], rid))
if reserved == nil then
  return {0, tonumber(redis.call('GET', KEYS[1])) or 0}
end
local used = redis.call('INCRBY', KEYS[1], actual - reserved)
redis.call('HDEL', KEYS[2], rid)
redis.call('ZREM', KEYS[3], rid)
return {1, used}
