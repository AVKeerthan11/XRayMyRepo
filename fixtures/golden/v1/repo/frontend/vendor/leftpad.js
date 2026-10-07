module.exports = function leftpad(str, len, ch) {
  str = String(str);
  ch = ch || " ";
  while (str.length < len) str = ch + str;
  return str;
};
