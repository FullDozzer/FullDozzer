package dev.revage.revagechat.stats;

import dev.revage.revagechat.chat.model.MessageContext;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.PriorityQueue;

/**
 * Runtime chat statistics with bounded-memory data structures.
 */
public final class StatisticsManager {
    private static final int MAX_PLAYERS = 512;
    private static final int MAX_WORDS = 2048;
    private static final int MAX_HOURLY_BUCKETS = 72;

    private final Map<String, Integer> messagesPerPlayer;
    private final Map<String, Integer> wordCounts;
    private final LinkedHashMap<Long, Integer> hourlyActivity;

    private int incomingCount;
    private int outgoingCount;
    private int hiddenMessagesCount;

    public StatisticsManager() {
        this.messagesPerPlayer = new HashMap<>(128);
        this.wordCounts = new HashMap<>(512);
        this.hourlyActivity = new LinkedHashMap<>(MAX_HOURLY_BUCKETS + 4, 0.75F, false) {
            @Override
            protected boolean removeEldestEntry(Map.Entry<Long, Integer> eldest) {
                return size() > MAX_HOURLY_BUCKETS;
            }
        };
    }

    public void recordIncoming() {
        incomingCount++;
        incrementHourlyBucket(Instant.now());
    }

    public void recordOutgoing() {
        outgoingCount++;
        incrementHourlyBucket(Instant.now());
    }

    public void recordIncoming(MessageContext context) {
        incomingCount++;
        recordPlayer(context.senderName());
        recordWords(context.formattedText());
        incrementHourlyBucket(context.timestamp());
    }

    public void recordOutgoing(MessageContext context) {
        outgoingCount++;
        recordPlayer(context.senderName());
        recordWords(context.formattedText());
        incrementHourlyBucket(context.timestamp());
    }

    public void recordHiddenMessage() {
        hiddenMessagesCount++;
    }

    public void markIncomingProcessed() {
        // Hook preserved for pipeline compatibility.
    }

    public void markOutgoingProcessed() {
        // Hook preserved for pipeline compatibility.
    }

    public int getIncomingCount() {
        return incomingCount;
    }

    public int getOutgoingCount() {
        return outgoingCount;
    }

    public int getHiddenMessagesCount() {
        return hiddenMessagesCount;
    }

    public List<Map.Entry<String, Integer>> topPlayers(int limit) {
        return topEntries(messagesPerPlayer, limit);
    }

    public List<Map.Entry<String, Integer>> topWords(int limit) {
        return topEntries(wordCounts, limit);
    }

    public String exportJson(int topPlayersLimit, int topWordsLimit) {
        StringBuilder json = new StringBuilder(1024);
        json.append('{');
        appendField(json, "incomingCount", incomingCount).append(',');
        appendField(json, "outgoingCount", outgoingCount).append(',');
        appendField(json, "hiddenMessagesCount", hiddenMessagesCount).append(',');

        json.append("\"messagesPerPlayer\":");
        appendArrayOfEntries(json, topPlayers(topPlayersLimit));
        json.append(',');

        json.append("\"topWords\":");
        appendArrayOfEntries(json, topWords(topWordsLimit));
        json.append(',');

        json.append("\"activityHourly\":[");
        boolean first = true;
        for (Map.Entry<Long, Integer> bucket : hourlyActivity.entrySet()) {
            if (!first) {
                json.append(',');
            }
            first = false;
            json.append('{')
                .append("\"hour\":").append(bucket.getKey()).append(',')
                .append("\"count\":").append(bucket.getValue())
                .append('}');
        }
        json.append(']');

        json.append('}');
        return json.toString();
    }

    private void recordPlayer(String senderName) {
        String key = normalize(senderName);
        messagesPerPlayer.merge(key, 1, Integer::sum);
        if (messagesPerPlayer.size() > MAX_PLAYERS) {
            pruneLowest(messagesPerPlayer, MAX_PLAYERS);
        }
    }

    private void recordWords(String message) {
        int start = -1;
        int length = message.length();

        for (int i = 0; i <= length; i++) {
            boolean boundary = i == length || !Character.isLetterOrDigit(message.charAt(i));
            if (boundary) {
                if (start >= 0) {
                    String token = message.substring(start, i).toLowerCase(Locale.ROOT);
                    if (token.length() >= 2) {
                        wordCounts.merge(token, 1, Integer::sum);
                    }
                    start = -1;
                }
            } else if (start < 0) {
                start = i;
            }
        }

        if (wordCounts.size() > MAX_WORDS) {
            pruneLowest(wordCounts, MAX_WORDS);
        }
    }

    private void incrementHourlyBucket(Instant timestamp) {
        long hour = timestamp.getEpochSecond() / 3600L;
        hourlyActivity.merge(hour, 1, Integer::sum);
    }

    private static String normalize(String value) {
        if (value == null || value.isBlank()) {
            return "unknown";
        }
        return value.toLowerCase(Locale.ROOT);
    }

    private static <K> void pruneLowest(Map<K, Integer> map, int targetSize) {
        int toRemove = map.size() - targetSize;
        if (toRemove <= 0) {
            return;
        }

        PriorityQueue<Map.Entry<K, Integer>> heap = new PriorityQueue<>(Comparator.comparingInt(Map.Entry::getValue));
        for (Map.Entry<K, Integer> entry : map.entrySet()) {
            if (heap.size() < toRemove) {
                heap.offer(entry);
            } else if (entry.getValue() > heap.peek().getValue()) {
                heap.poll();
                heap.offer(entry);
            }
        }

        while (!heap.isEmpty()) {
            map.remove(heap.poll().getKey());
        }
    }

    private static List<Map.Entry<String, Integer>> topEntries(Map<String, Integer> source, int limit) {
        if (limit <= 0 || source.isEmpty()) {
            return List.of();
        }

        PriorityQueue<Map.Entry<String, Integer>> heap = new PriorityQueue<>(Comparator.comparingInt(Map.Entry::getValue));
        for (Map.Entry<String, Integer> entry : source.entrySet()) {
            if (heap.size() < limit) {
                heap.offer(entry);
            } else if (entry.getValue() > heap.peek().getValue()) {
                heap.poll();
                heap.offer(entry);
            }
        }

        ArrayList<Map.Entry<String, Integer>> result = new ArrayList<>(heap);
        result.sort((a, b) -> Integer.compare(b.getValue(), a.getValue()));
        return result;
    }

    private static StringBuilder appendField(StringBuilder json, String name, int value) {
        return json.append('\"').append(name).append("\":").append(value);
    }

    private static void appendArrayOfEntries(StringBuilder json, List<Map.Entry<String, Integer>> entries) {
        json.append('[');
        for (int i = 0; i < entries.size(); i++) {
            if (i > 0) {
                json.append(',');
            }

            Map.Entry<String, Integer> entry = entries.get(i);
            json.append('{')
                .append("\"name\":\"").append(escapeJson(entry.getKey())).append("\",")
                .append("\"count\":").append(entry.getValue())
                .append('}');
        }
        json.append(']');
    }

    private static String escapeJson(String value) {
        StringBuilder escaped = new StringBuilder(value.length() + 8);
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            if (c == '"' || c == '\\') {
                escaped.append('\\');
            }
            escaped.append(c);
        }
        return escaped.toString();
    }
}
