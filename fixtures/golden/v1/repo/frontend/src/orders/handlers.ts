import type { Request, Response } from "express";

import type { Order } from "./types";

export function getOrder(req: Request, res: Response): void {
  const order: Order = { id: req.params.id, total: 0 };
  res.json(order);
}
