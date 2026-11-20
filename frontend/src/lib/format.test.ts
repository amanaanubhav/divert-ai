import { describe, expect, it } from 'vitest'
import {
  formatClock,
  formatDistance,
  formatDuration,
  formatSeconds,
  formatSpeed,
  secondsBetween,
} from './format'
import { addDays, presetRange, tzOffsetMinutes, zonedDay, zonedDayStartIso } from './time'

describe('format', () => {
  it('distances', () => {
    expect(formatDistance(812.4)).toBe('812 m')
    expect(formatDistance(1090.2)).toBe('1.09 km')
    expect(formatDistance(18_873)).toBe('18.9 km')
    expect(formatDistance(null)).toBe('–')
  })

  it('durations', () => {
    expect(formatDuration(73.4)).toBe('1:13')
    expect(formatDuration(3725)).toBe('1:02:05')
    expect(formatDuration(-3)).toBe('0:00')
    expect(formatSeconds(45)).toBe('45 s')
    expect(formatSeconds(600)).toBe('10.0 min')
    expect(formatSpeed(22.22)).toBe('80 km/h')
  })

  it('clock in the city time zone (IST = UTC+05:30)', () => {
    expect(formatClock('2026-11-17T06:30:00+00:00', 'Asia/Kolkata')).toBe('12:00:00')
    expect(formatClock(null, 'Asia/Kolkata')).toBe('--:--:--')
  })

  it('seconds between ISO times', () => {
    expect(secondsBetween('2026-11-17T06:30:00Z', '2026-11-17T06:31:15.5Z')).toBe(75.5)
    expect(secondsBetween(null, '2026-11-17T06:31:15Z')).toBeNull()
  })
})

describe('time zones', () => {
  it('offset of Asia/Kolkata is +330 min', () => {
    expect(tzOffsetMinutes(new Date('2026-11-17T00:00:00Z'), 'Asia/Kolkata')).toBe(330)
  })

  it('local midnight in the city as an instant', () => {
    expect(zonedDayStartIso('2026-11-17', 'Asia/Kolkata')).toBe('2026-11-16T18:30:00.000Z')
    expect(zonedDayStartIso('2026-11-17', 'UTC')).toBe('2026-11-17T00:00:00.000Z')
  })

  it('handles a DST zone', () => {
    // New York is UTC-4 in July
    expect(zonedDayStartIso('2026-07-01', 'America/New_York')).toBe('2026-07-01T04:00:00.000Z')
  })

  it('days and ranges', () => {
    expect(zonedDay(new Date('2026-11-16T19:00:00Z'), 'Asia/Kolkata')).toBe('2026-11-17')
    expect(addDays('2026-12-31', 1)).toBe('2027-01-01')
    const r = presetRange('24h', Date.parse('2026-11-17T06:30:42Z'))
    expect(r.to).toBe('2026-11-17T06:31:00.000Z')
    expect(r.from).toBe('2026-11-16T06:31:00.000Z')
  })
})
