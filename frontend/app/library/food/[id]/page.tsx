import FoodDetail from "@/components/FoodDetail";

/** §7.4 food detail — drill-in from Library or any diary row
 *  (link target: /library/food/<foods.id>). */
export default async function FoodDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <FoodDetail id={id} />;
}
