-- Generic Cell Rate Algorithm rate limiter.
-- KEYS[1] = theoretical arrival time (TAT) key
-- ARGV[1] = emission interval in ms (ms per token)
-- ARGV[2] = burst capacity in tokens
-- ARGV[3] = cost of this request in tokens
-- ARGV[4] = optional now in ms (tests); empty string -> server TIME
-- Returns {allowed(0|1), retry_after_ms, remaining_tokens}
local emission = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local now
if ARGV[4] ~= nil and ARGV[4] ~= '' then
  now = tonumber(ARGV[4])
else
  local t = redis.call('TIME')
  now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
end

local tat = tonumber(redis.call('GET', KEYS[1]))
if tat == nil or tat < now then tat = now end

local new_tat = tat + emission * cost
local allow_at = new_tat - burst * emission
if now < allow_at then
  local remaining = math.floor((now - (tat - burst * emission)) / emission)
  if remaining < 0 then remaining = 0 end
  return {0, allow_at - now, remaining}
end

redis.call('SET', KEYS[1], new_tat, 'PX', math.max(1, math.ceil(new_tat - now)))
local remaining = math.floor((now - (new_tat - burst * emission)) / emission)
return {1, 0, remaining}
