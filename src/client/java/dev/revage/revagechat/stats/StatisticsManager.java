package dev.revage.revagechat.stats;

/**
 * Tracks lightweight runtime counters for chat activity.
 */
public final class StatisticsManager {
    private int incomingCount;
    private int outgoingCount;

    public void recordIncoming() {
        incomingCount++;
    }

    public void recordOutgoing() {
        outgoingCount++;
    }

    public void markIncomingProcessed() {
        // TODO: Add richer metrics and persistence strategy.
    }

    public void markOutgoingProcessed() {
        // TODO: Add richer metrics and persistence strategy.
    }

    public int getIncomingCount() {
        return incomingCount;
    }

    public int getOutgoingCount() {
        return outgoingCount;
    }
}
