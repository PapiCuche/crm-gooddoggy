import { AiProviderAccounts } from "@/components/ai/ai-provider-accounts";
import { AiSwitchPanel } from "@/components/ai/ai-switch";

export default function AiPage() {
  return (
    <AiProviderAccounts>
      <AiSwitchPanel />
    </AiProviderAccounts>
  );
}
