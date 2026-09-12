"""Objeto de valor Money. El dinero se maneja en centimos enteros, nunca en float."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Money:
    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.amount_minor, int):
            raise TypeError("amount_minor debe ser entero (centimos)")
        if len(self.currency) != 3:
            raise ValueError("currency debe ser ISO-4217 (3 letras)")

    def _same_currency(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise ValueError("No se pueden operar montos de distinta moneda")

    def __add__(self, other: "Money") -> "Money":
        self._same_currency(other)
        return Money(self.amount_minor + other.amount_minor, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._same_currency(other)
        return Money(self.amount_minor - other.amount_minor, self.currency)
