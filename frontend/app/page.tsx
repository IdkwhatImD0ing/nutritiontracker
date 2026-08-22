import TabBar from "@/components/TabBar";
import Today from "@/components/Today";
import WizardGate from "@/components/WizardGate";

/** §7.4 Today: the capture bar + staging + diary, the three-tab bar, and the
 *  first-run goal wizard (renders nothing once settings.targets exist). */
export default function Home() {
  return (
    <>
      <Today />
      <WizardGate />
      <TabBar />
    </>
  );
}
