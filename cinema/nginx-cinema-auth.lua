-- Shared by the session endpoint and every private static resource.
-- AList can return HTTP 200 with an application-level 401: inspect the JSON.
local json = require('cjson.safe')
local session = ngx.var.uri == '/cinema/session'
ngx.header['Cache-Control'] = 'private, no-store'

if session and ngx.req.get_method() ~= 'GET' then
    return ngx.exit(ngx.HTTP_NOT_ALLOWED)
end

local token = ngx.var.http_authorization
if not token or token == '' then
    token = ngx.unescape_uri(ngx.var.cookie_cinema_session or '')
end
local function deny(status)
    if session then
        ngx.header['Set-Cookie'] = 'cinema_session=; Path=/cinema/; Max-Age=0; HttpOnly; Secure; SameSite=Strict'
    end
    return ngx.exit(status)
end
if token == '' or #token > 8192 or token:find('[%c]') then
    return deny(ngx.HTTP_UNAUTHORIZED)
end

ngx.var.cinema_token = token
local response = ngx.location.capture('/_cinema_identity', {
    method = ngx.HTTP_GET, vars = { cinema_token = token },
})
if response.status ~= ngx.HTTP_OK then
    return deny(ngx.HTTP_SERVICE_UNAVAILABLE)
end
local data = json.decode(response.body)
if type(data) ~= 'table' then return deny(ngx.HTTP_SERVICE_UNAVAILABLE) end
if data.code ~= 200 or type(data.data) ~= 'table' then
    return deny(ngx.HTTP_UNAUTHORIZED)
end
local user = data.data
if user.disabled == true or user.disabled == 1 or type(user.role) ~= 'table' or #user.role == 0 then
    return deny(ngx.HTTP_UNAUTHORIZED)
end
for _, role in ipairs(user.role) do
    if role == 1 then return deny(ngx.HTTP_UNAUTHORIZED) end -- AList guest role
end

if session then
    ngx.header['Set-Cookie'] = 'cinema_session=' .. ngx.escape_uri(token) .. '; Path=/cinema/; HttpOnly; Secure; SameSite=Strict'
    ngx.header['Content-Type'] = 'application/json; charset=utf-8'
    ngx.say('{"authenticated":true}')
    return ngx.exit(ngx.HTTP_OK)
end
