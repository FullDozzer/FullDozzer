package dev.revage.revagechat.mixin;

import dev.revage.revagechat.RevageChatClient;
import net.minecraft.client.gui.screen.ChatScreen;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/**
 * Allows dragging/resizing RevageChat windows while chat screen is open.
 */
@Mixin(ChatScreen.class)
public abstract class ChatScreenMixin {
    @Inject(method = "mouseClicked", at = @At("HEAD"), cancellable = true)
    private void revagechat$mouseClicked(double mouseX, double mouseY, int button, CallbackInfoReturnable<Boolean> cir) {
        RevageChatClient client = RevageChatClient.instance();
        if (client != null && client.onWindowMouseDown(mouseX, mouseY, button)) {
            cir.setReturnValue(true);
        }
    }

    @Inject(method = "mouseDragged", at = @At("HEAD"), cancellable = true)
    private void revagechat$mouseDragged(double mouseX, double mouseY, int button, double deltaX, double deltaY, CallbackInfoReturnable<Boolean> cir) {
        RevageChatClient client = RevageChatClient.instance();
        if (client != null && client.onWindowMouseDrag(mouseX, mouseY, button)) {
            cir.setReturnValue(true);
        }
    }

    @Inject(method = "mouseReleased", at = @At("HEAD"), cancellable = true)
    private void revagechat$mouseReleased(double mouseX, double mouseY, int button, CallbackInfoReturnable<Boolean> cir) {
        RevageChatClient client = RevageChatClient.instance();
        if (client != null && client.onWindowMouseUp(button)) {
            cir.setReturnValue(true);
        }
    }

    @Inject(method = "mouseScrolled", at = @At("HEAD"), cancellable = true)
    private void revagechat$mouseScrolled(double mouseX, double mouseY, double horizontalAmount, double verticalAmount, CallbackInfoReturnable<Boolean> cir) {
        RevageChatClient client = RevageChatClient.instance();
        if (client != null && client.onWindowMouseScroll(mouseX, mouseY, verticalAmount)) {
            cir.setReturnValue(true);
        }
    }
}
