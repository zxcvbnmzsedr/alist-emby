import test from 'node:test';
import assert from 'node:assert/strict';
import { isLibraryUser } from '../src/auth.js';

test('允许数字和数组形式的管理员及普通账号角色', () => {
  for (const role of [2, 0, [2], [0], [0, 2]]) {
    assert.equal(isLibraryUser({role, disabled:false}), true, JSON.stringify(role));
  }
});

test('拒绝访客、混合访客角色和禁用账号', () => {
  for (const role of [1, [1], [2, 1]]) {
    assert.equal(isLibraryUser({role, disabled:false}), false, JSON.stringify(role));
  }
  for (const role of [2, [2]]) {
    for (const disabled of [true, 1]) {
      assert.equal(isLibraryUser({role, disabled}), false);
    }
  }
});

test('拒绝缺失、空或类型错误的角色', () => {
  assert.equal(isLibraryUser(null), false);
  for (const role of [undefined, null, [], {}, '2', ['2'], [2, '0'], false, -1, 2.5, NaN, Infinity]) {
    assert.equal(isLibraryUser({role}), false, String(role));
  }
});
