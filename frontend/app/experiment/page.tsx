import type { Metadata } from "next";
import Experiment from "@/components/Experiment";

// Bare leaf: the root layout's "%s · Tally" template adds the suffix.
export const metadata: Metadata = { title: "Experiment" };

export default function ExperimentPage() {
  return <Experiment />;
}
