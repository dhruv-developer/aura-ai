-- Release a reservation without consuming it (request failed / cancelled).
-- KEYS as quota_reserve. ARGV[1] = reservation id
-- Returns {ok(0|1), used_after}
local rid = ARGV[1]
local reserved = tonumber(redis.call('HGET', KEYS[2], rid))
if reserved == nil then
  return {0, tonumber(redis.call('GET', KEYS[1])) or 0}
end
local used = redis.call('DECRBY', KEYS[1], reserved)
redis.call('HDEL', KEYS[2], rid)
redis.call('ZREM', KEYS[3], rid)
return {1, used}
