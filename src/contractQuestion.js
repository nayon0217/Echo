/**
 * Detect questions about a worker's own employment document.
 *
 * These must not go through the MOM policy path — they can only be answered from
 * the contract itself. Used to prompt for an upload, then hold the document.
 */

const POLICY_ONLY = /\b(levy|quota|work permit levy|scamshield|scam)\b/i;

const CONTRACT_HINTS =
  /\b(contract|kontrak|offer letter|\bipa\b|in-?principle approval|notice period|basic salary|my salary|my pay|my wage|my hours|deduct(?:ion)?s?|overtime|rest day|working hours|annual leave|sick leave|how much (?:do i|i) (?:earn|get|make)|can they deduct|can my employer|pemotongan|gaji|jam kerja|cuti|\u5de5\u8d44|\u5408\u540c|\u85aa\u6c34|\u901a\u77e5\u671f|\u09ac\u09c7\u09a4\u09a8|\u099a\u09c1\u0995\u09cd\u09a4\u09bf|\u0b9a\u0bae\u0bcd\u0baa\u0bb3\u0bae\u0bcd|\u0b92\u0baa\u0bcd\u0baa\u0ba8\u0bcd\u0ba4\u0bae\u0bcd)\b/i;

/**
 * @param {string} text
 * @returns {boolean}
 */
export function isContractQuestion(text) {
  const cleaned = String(text || "").trim();
  if (cleaned.length < 4) return false;
  if (POLICY_ONLY.test(cleaned) && !/\b(contract|my (?:salary|pay|wage|hours))\b/i.test(cleaned)) {
    return false;
  }
  return CONTRACT_HINTS.test(cleaned);
}
