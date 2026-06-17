/* Solar elevation for automatic day/night switching — like a car nav app, but computed
   from our GPS position + UTC time (iPad Safari can't read an ambient-light sensor).
   Approximate NOAA low-precision algorithm; accurate to a fraction of a degree — plenty
   to flip the theme around dusk/dawn. */
(function (g) {
  const rad = Math.PI / 180;
  function norm(d) { return ((d % 360) + 360) % 360; }

  // Sun elevation above the horizon (degrees) at lat/lon (deg, E+) and a JS Date (UTC).
  function solarElevation(lat, lon, date) {
    const jd = date.getTime() / 86400000 + 2440587.5;   // Julian date
    const d = jd - 2451545.0;                             // days since J2000.0
    const gM = norm(357.529 + 0.98560028 * d);            // mean anomaly
    const q = norm(280.459 + 0.98564736 * d);             // mean longitude
    const L = norm(q + 1.915 * Math.sin(gM * rad) + 0.020 * Math.sin(2 * gM * rad)); // ecliptic lon
    const e = 23.439 - 0.00000036 * d;                    // obliquity
    const RA = Math.atan2(Math.cos(e * rad) * Math.sin(L * rad), Math.cos(L * rad)) / rad;
    const decl = Math.asin(Math.sin(e * rad) * Math.sin(L * rad)) / rad;  // declination
    const GMST = norm(280.46061837 + 360.98564736629 * d);
    const LST = norm(GMST + lon);                         // local sidereal time
    let H = LST - RA;                                     // hour angle
    H = ((H + 180) % 360) - 180;
    return Math.asin(Math.sin(lat * rad) * Math.sin(decl * rad) +
      Math.cos(lat * rad) * Math.cos(decl * rad) * Math.cos(H * rad)) / rad;
  }

  // Daylight if the sun is above civil twilight (-6°): switch to night theme at dusk.
  function isDaylight(lat, lon, date, twilightDeg) {
    return solarElevation(lat, lon, date || new Date()) > (twilightDeg == null ? -6 : twilightDeg);
  }

  g.Sun = { solarElevation, isDaylight };
})(window);
