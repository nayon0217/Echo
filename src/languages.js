// The language options ECHO offers on first contact.
// WhatsApp interactive "reply button" messages allow a maximum of 3 buttons,
// so with 4 options we use an interactive "list" message instead.
export const LANGUAGES = [
  { id: "lang_en", code: "en", title: "English", description: "Reply in English" },
  { id: "lang_bn", code: "bn", title: "বাংলা (Bengali)", description: "বাংলায় উত্তর দিন" },
  { id: "lang_ta", code: "ta", title: "தமிழ் (Tamil)", description: "தமிழில் பதிலளிக்கவும்" },
  { id: "lang_zh", code: "zh", title: "中文 (Mandarin)", description: "用中文回复" },
];

export const LANGUAGE_BY_ID = Object.fromEntries(
  LANGUAGES.map((lang) => [lang.id, lang]),
);
