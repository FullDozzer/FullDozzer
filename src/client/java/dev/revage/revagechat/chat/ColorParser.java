package dev.revage.revagechat.chat;

import java.util.LinkedHashMap;
import java.util.Map;
import net.minecraft.text.MutableText;
import net.minecraft.text.Style;
import net.minecraft.text.Text;
import net.minecraft.text.TextColor;
import org.jetbrains.annotations.Nullable;

/**
 * Parses color tokens for chat messages.
 *
 * <p>Supported tokens:
 * <ul>
 *   <li>Legacy '&' codes: &0-&9, &a-&f, &g (mapped to gold)</li>
 *   <li>Hex colors: #RRGGBB</li>
 * </ul>
 */
public final class ColorParser {
    private static final int CACHE_SIZE = 128;

    private static final Map<String, Integer> HEX_COLOR_CACHE = new LinkedHashMap<>(CACHE_SIZE, 0.75f, true) {
        @Override
        protected boolean removeEldestEntry(Map.Entry<String, Integer> eldest) {
            return size() > CACHE_SIZE;
        }
    };

    private ColorParser() {
    }

    public static boolean hasExplicitColor(String input) {
        int length = input.length();

        for (int i = 0; i < length; i++) {
            char current = input.charAt(i);

            if (current == '&' && i + 1 < length && isLegacyColorCode(input.charAt(i + 1))) {
                return true;
            }

            if (current == '#' && i + 6 < length && tryParseHexColor(input, i + 1) != null) {
                return true;
            }
        }

        return false;
    }

    public static String toMinecraftFormatting(
        String input,
        @Nullable Integer fallbackRgb,
        boolean overrideIfColored
    ) {
        if (!overrideIfColored && hasExplicitColor(input)) {
            return input;
        }

        StringBuilder builder = new StringBuilder(input.length() + 16);

        if (fallbackRgb != null) {
            appendSectionHex(builder, fallbackRgb);
        }

        int length = input.length();
        for (int i = 0; i < length; i++) {
            char current = input.charAt(i);

            if (current == '&' && i + 1 < length) {
                char code = Character.toLowerCase(input.charAt(i + 1));
                if (isLegacyColorCode(code)) {
                    builder.append('§').append(mapLegacyCode(code));
                    i++;
                    continue;
                }
            }

            if (current == '#' && i + 6 < length) {
                Integer color = tryParseHexColor(input, i + 1);
                if (color != null) {
                    appendSectionHex(builder, color);
                    i += 6;
                    continue;
                }
            }

            builder.append(current);
        }

        return builder.toString();
    }

    public static MutableText toColoredText(
        String input,
        @Nullable Integer fallbackRgb,
        boolean overrideIfColored
    ) {
        if (!overrideIfColored && hasExplicitColor(input)) {
            return Text.literal(input);
        }

        MutableText root = Text.empty();
        StringBuilder segment = new StringBuilder(input.length());

        TextColor activeColor = fallbackRgb == null ? null : TextColor.fromRgb(fallbackRgb);

        int length = input.length();
        for (int i = 0; i < length; i++) {
            char current = input.charAt(i);

            if (current == '&' && i + 1 < length) {
                char code = Character.toLowerCase(input.charAt(i + 1));
                if (isLegacyColorCode(code)) {
                    flushSegment(root, segment, activeColor);
                    activeColor = TextColor.fromRgb(legacyRgb(mapLegacyCode(code)));
                    i++;
                    continue;
                }
            }

            if (current == '#' && i + 6 < length) {
                Integer color = tryParseHexColor(input, i + 1);
                if (color != null) {
                    flushSegment(root, segment, activeColor);
                    activeColor = TextColor.fromRgb(color);
                    i += 6;
                    continue;
                }
            }

            segment.append(current);
        }

        flushSegment(root, segment, activeColor);
        return root;
    }

    private static void flushSegment(MutableText root, StringBuilder segment, @Nullable TextColor color) {
        if (segment.isEmpty()) {
            return;
        }

        MutableText part = Text.literal(segment.toString());
        if (color != null) {
            part.setStyle(Style.EMPTY.withColor(color));
        }

        root.append(part);
        segment.setLength(0);
    }

    private static void appendSectionHex(StringBuilder builder, int rgb) {
        builder.append('§').append('x');

        for (int shift = 20; shift >= 0; shift -= 4) {
            int nibble = (rgb >> shift) & 0xF;
            char hex = Character.forDigit(nibble, 16);
            builder.append('§').append(hex);
        }
    }

    @Nullable
    private static Integer tryParseHexColor(String input, int startInclusive) {
        String key = input.substring(startInclusive, startInclusive + 6).toLowerCase();
        Integer cached = HEX_COLOR_CACHE.get(key);
        if (cached != null) {
            return cached;
        }

        int value = 0;
        for (int i = 0; i < 6; i++) {
            int digit = Character.digit(key.charAt(i), 16);
            if (digit < 0) {
                return null;
            }

            value = (value << 4) | digit;
        }

        HEX_COLOR_CACHE.put(key, value);
        return value;
    }

    private static boolean isLegacyColorCode(char code) {
        return (code >= '0' && code <= '9') || (code >= 'a' && code <= 'f') || code == 'g';
    }

    private static char mapLegacyCode(char code) {
        return code == 'g' ? '6' : code;
    }

    private static int legacyRgb(char code) {
        return switch (code) {
            case '0' -> 0x000000;
            case '1' -> 0x0000AA;
            case '2' -> 0x00AA00;
            case '3' -> 0x00AAAA;
            case '4' -> 0xAA0000;
            case '5' -> 0xAA00AA;
            case '6' -> 0xFFAA00;
            case '7' -> 0xAAAAAA;
            case '8' -> 0x555555;
            case '9' -> 0x5555FF;
            case 'a' -> 0x55FF55;
            case 'b' -> 0x55FFFF;
            case 'c' -> 0xFF5555;
            case 'd' -> 0xFF55FF;
            case 'e' -> 0xFFFF55;
            case 'f' -> 0xFFFFFF;
            default -> 0xFFFFFF;
        };
    }
}
