// The language options ECHO offers on first contact.
// WhatsApp interactive "reply button" messages allow a maximum of 3 buttons,
// so with 6+ options we use an interactive "list" message instead.
// Row description is the short language name only (not "Reply in …").
export const LANGUAGES = [
  // No description for English — WhatsApp shows title+description on select,
  // so "English"/"English" appeared as "English English".
  { id: "lang_en", code: "en", title: "English", description: "" },
  { id: "lang_id", code: "id", title: "Bahasa Indonesia", description: "Indonesia" },
  { id: "lang_my", code: "my", title: "မြန်မာ (Burmese)", description: "မြန်မာ" },
  { id: "lang_bn", code: "bn", title: "বাংলা (Bengali)", description: "বাংলা" },
  { id: "lang_ta", code: "ta", title: "தமிழ் (Tamil)", description: "தமிழ்" },
  { id: "lang_zh", code: "zh", title: "中文 (Mandarin)", description: "中文" },
];

export const LANGUAGE_BY_ID = Object.fromEntries(
  LANGUAGES.map((lang) => [lang.id, lang]),
);

/** Post-language-choice prompt, in the worker's chosen language. */
export const WELCOME_AFTER_LANGUAGE = {
  en: "Great — I'll reply in English. Send me a voice note, image, or message to check.",
  id: "Baik — saya akan menjawab dalam Bahasa Indonesia. Kirim catatan suara, gambar, atau pesan untuk diperiksa.",
  my: "ကောင်းပါပြီ — ကျွန်ုပ် မြန်မာဘာသာဖြင့် ပြန်ကြားပါမည်။ စစ်ဆေးရန် အသံမှတ်တမ်း၊ ပုံ သို့မဟုတ် စာတို ပို့ပေးပါ။",
  bn: "ঠিক আছে — আমি বাংলায় উত্তর দেব। যাচাই করার জন্য একটি ভয়েস নোট, ছবি বা বার্তা পাঠান।",
  ta: "சரி — நான் தமிழில் பதிலளிப்பேன். சரிபார்க்க ஒரு குரல் குறிப்பு, படம் அல்லது செய்தியை அனுப்புங்கள்.",
  zh: "好的 — 我会用中文回复。请发语音、图片或文字消息给我查证。",
};

/** In-progress acknowledgement while the pipeline runs. */
export const CHECKING_MESSAGE = {
  en: "🔎 Checking your message…",
  id: "🔎 Sedang memeriksa pesan Anda…",
  my: "🔎 သင့်စာတိုကို စစ်ဆေးနေပါသည်…",
  bn: "🔎 আপনার বার্তা যাচাই করা হচ্ছে…",
  ta: "🔎 உங்கள் செய்தியை சரிபார்க்கிறோம்…",
  zh: "🔎 正在查证您的消息…",
};

/** Short labels shown above transcribed / OCR text. */
export const HEARD_PREFIX = {
  en: "Heard:",
  id: "Terdengar:",
  my: "ကြားရသည်:",
  bn: "শুনেছি:",
  ta: "கேட்டது:",
  zh: "听到：",
};

export const IMAGE_TEXT_PREFIX = {
  en: "Image text:",
  id: "Teks gambar:",
  my: "ပုံစာသား:",
  bn: "ছবির লেখা:",
  ta: "பட உரை:",
  zh: "图片文字：",
};

export function uiString(map, code) {
  return map[code] || map.en;
}
