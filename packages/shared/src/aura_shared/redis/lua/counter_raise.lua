-- Raise a counter to at least ARGV[1] (never lowers it). Used by the usage reconciler to
-- rebuild Redis from the Postgres snapshot after data loss.
-- KEYS[1] = counter, ARGV[1] = floor, ARGV[2] = expiry ms if the key is created (0 = none)
-- Returns the resulting value.
local cur = tonumber(redis.call('GET', KEYS[1]))
local floor = tonumber(ARGV[1])
if cur == nil or cur < floor then
  if tonumber(ARGV[2]) > 0 then
    redis.call('SET', KEYS[1], floor, 'PX', ARGV[2])
  else
    redis.call('SET', KEYS[1], floor, 'KEEPTTL')
  end
  return floor
end
return cur
