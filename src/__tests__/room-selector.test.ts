import { describe, expect, it } from 'vitest';

import { selectRooms } from '../room-selector.js';

const rooms = [
  { room_id: 70000001, anchor_name: '星河' },
  { room_id: 20001, anchor_name: '小熊' },
  { room_id: 20002, anchor_name: '小熊今天直播' },
  { room_id: 30001, anchor_name: '银河小熊' },
];

describe('selectRooms', () => {
  it('空字符串查询返回空数组', () => {
    expect(selectRooms(rooms, '')).toEqual([]);
  });

  it('精确名称匹配优先于部分匹配', () => {
    expect(selectRooms(rooms, '小熊')).toEqual([
      { room_id: 20001, anchor_name: '小熊' },
    ]);
  });

  it('部分关键词匹配多个主播', () => {
    expect(selectRooms(rooms, '熊')).toEqual([
      { room_id: 20001, anchor_name: '小熊' },
      { room_id: 20002, anchor_name: '小熊今天直播' },
      { room_id: 30001, anchor_name: '银河小熊' },
    ]);
  });

  it('全角数字房间号经 NFKC 归一化后正确匹配', () => {
    expect(selectRooms(rooms, '７００００００１')).toEqual([
      { room_id: 70000001, anchor_name: '星河' },
    ]);
  });

  it('纯数字字符串按 room_id 精确匹配', () => {
    const numericRooms = [
      ...rooms,
      { room_id: 456, anchor_name: '70000001号机' },
    ];

    expect(selectRooms(numericRooms, '70000001')).toEqual([
      { room_id: 70000001, anchor_name: '星河' },
    ]);
  });
});
