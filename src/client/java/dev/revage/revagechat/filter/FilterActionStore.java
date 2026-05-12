package dev.revage.revagechat.filter;

import dev.revage.revagechat.chat.model.MessageContext;
import java.util.Collections;
import java.util.Map;
import java.util.Set;
import java.util.WeakHashMap;

/**
 * Weakly-referenced side-channel store for filter outcomes.
 */
public final class FilterActionStore {
    private static final Map<MessageContext, String> overrideText = Collections.synchronizedMap(new WeakHashMap<>());
    private static final Set<MessageContext> blocked = Collections.newSetFromMap(Collections.synchronizedMap(new WeakHashMap<>()));
    private static final Map<MessageContext, String> tag = Collections.synchronizedMap(new WeakHashMap<>());
    private static final Map<MessageContext, Integer> recolor = Collections.synchronizedMap(new WeakHashMap<>());
    private static final Map<MessageContext, String> autoReply = Collections.synchronizedMap(new WeakHashMap<>());
    private static final Map<MessageContext, String> autoCommand = Collections.synchronizedMap(new WeakHashMap<>());

    private FilterActionStore() {
    }

    public static String text(MessageContext ctx) {
        return overrideText.getOrDefault(ctx, ctx.formattedText());
    }

    public static void setText(MessageContext ctx, String text) {
        overrideText.put(ctx, text);
    }

    public static void block(MessageContext ctx) {
        blocked.add(ctx);
    }

    public static boolean isBlocked(MessageContext ctx) {
        return blocked.contains(ctx);
    }

    public static void setTag(MessageContext ctx, String value) {
        tag.put(ctx, value);
    }

    public static String tag(MessageContext ctx) {
        return tag.get(ctx);
    }

    public static void setRecolor(MessageContext ctx, int rgb) {
        recolor.put(ctx, rgb);
    }

    public static Integer recolor(MessageContext ctx) {
        return recolor.get(ctx);
    }

    public static void setAutoReply(MessageContext ctx, String value) {
        autoReply.put(ctx, value);
    }

    public static String autoReply(MessageContext ctx) {
        return autoReply.get(ctx);
    }

    public static void setAutoCommand(MessageContext ctx, String value) {
        autoCommand.put(ctx, value);
    }

    public static String autoCommand(MessageContext ctx) {
        return autoCommand.get(ctx);
    }

    public static void clear(MessageContext ctx) {
        overrideText.remove(ctx);
        blocked.remove(ctx);
        tag.remove(ctx);
        recolor.remove(ctx);
        autoReply.remove(ctx);
        autoCommand.remove(ctx);
    }
}
