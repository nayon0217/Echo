/**
 * Classifies an inbound WhatsApp message into the kind of thing ECHO can act on.
 *
 * Pure — no I/O, no side effects — so the routing decision is testable on its own,
 * separately from the handlers that act on it (see src/handlers.js).
 *
 * WhatsApp reports many message types (video, document, sticker, location,
 * contacts, reaction, order, system, unknown). Only text, image, and voice are in
 * scope; everything else is ignored per product decision.
 */

export const KIND = {
  /** A typed message. Goes through the translate pipeline today. */
  TEXT: "text",
  /** A photo or screenshot — job ad, "MOM letter", forwarded poster. Not built yet. */
  IMAGE: "image",
  /** A voice note or audio file. Not built yet. */
  VOICE: "voice",
  /** A menu tap or button press. Not media — drives the conversation. */
  CONTROL: "control",
  /** Anything else. Deliberately ignored. */
  UNSUPPORTED: "unsupported",
};

/**
 * @typedef {object} Classification
 * @property {string}  kind        one of KIND
 * @property {string}  type        the raw WhatsApp message type, for logging
 * @property {string=} text        TEXT only: the body
 * @property {string=} mediaId     IMAGE/VOICE: the media id to download (not the bytes)
 * @property {string=} mimeType    IMAGE/VOICE: e.g. "audio/ogg; codecs=opus"
 * @property {string=} caption     IMAGE: caption typed alongside the image, if any
 * @property {boolean=} isVoiceNote VOICE: true if recorded in-app, false if an uploaded file
 * @property {string=} replyId     CONTROL: the id of the tapped list row or button
 */

/**
 * Decide what kind of message this is.
 *
 * @param {object} message a `messages[]` entry from the Meta webhook payload
 * @returns {Classification}
 */
export function classify(message) {
  const type = message?.type ?? "unknown";

  switch (type) {
    case "text":
      return { kind: KIND.TEXT, type, text: message.text?.body ?? "" };

    case "image":
      return {
        kind: KIND.IMAGE,
        type,
        mediaId: message.image?.id,
        mimeType: message.image?.mime_type,
        // An image often carries the actual claim in its caption; keep it so the
        // image handler can use it rather than re-deriving it later.
        caption: message.image?.caption ?? "",
      };

    case "audio":
      return {
        kind: KIND.VOICE,
        type,
        mediaId: message.audio?.id,
        mimeType: message.audio?.mime_type,
        // Meta sets `voice: true` for in-app recordings and omits it for uploaded
        // audio files. Both are transcribable; the flag is worth keeping because a
        // forwarded file and a self-recorded note mean different things.
        isVoiceNote: Boolean(message.audio?.voice),
      };

    case "interactive":
      return {
        kind: KIND.CONTROL,
        type,
        replyId:
          message.interactive?.list_reply?.id ??
          message.interactive?.button_reply?.id,
      };

    case "button":
      return { kind: KIND.CONTROL, type, replyId: message.button?.payload };

    default:
      // video, document, sticker, location, contacts, reaction, order, system,
      // unknown, and anything Meta adds later.
      return { kind: KIND.UNSUPPORTED, type };
  }
}
