-- Release a lock/lease only if we still own it (fencing by token).
-- KEYS[1] = lease key, ARGV[1] = owner token. Returns 1 if released.
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
