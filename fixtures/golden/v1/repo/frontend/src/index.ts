import express from "express";

import { getOrder } from "./orders";

const app = express();

app.get("/orders/:id", getOrder);

app.listen(3000);
