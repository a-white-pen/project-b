"""
Shared data model for menu items across all sources.
Each scraper (fitfuel, jones, wongnai) returns list[MenuItem].
The runner collects these and writes them to external_data.menu_items.

Functions:
  MenuItem.__str__ — returns a compact debug string for standalone scraper output
"""

from dataclasses import dataclass, field


@dataclass
class MenuItem:
    source: str                    # 'fitfuel' | 'jones' | 'wongnai'
    restaurant_name: str           # canonical English name, hardcoded in scraper config
    item_name_en: str | None       # Best available name — English from source, or Thai script when no English available
    category: str | None           # menu section from source
    price_thb: float | None        # price in THB; null for Jones and future SGD menus
    price_sgd: float | None        # price in SGD; converted or native for SG menus
    kcal: float | None
    protein_g: float | None
    carbs_g: float | None
    fat_g: float | None
    fibre_g: float | None = None   # from FitFuel API; null for most others
    sugar_g: float | None = None   # not published by current sources
    sodium_mg: float | None = None # not published by current sources
    meta: dict = field(default_factory=dict)

    # Formats one menu item for standalone scraper debug output.
    # Input is the dataclass fields; output goes to CLI print statements.
    def __str__(self) -> str:
        name = self.item_name_en or "?"
        price = f"฿{self.price_thb:.0f}" if self.price_thb else "—"
        if self.kcal is None and self.protein_g is None and self.carbs_g is None and self.fat_g is None:
            macros = "no macros"
        else:
            parts = []
            if self.kcal is not None:
                parts.append(f"{self.kcal:.0f} kcal")
            if self.protein_g is not None:
                parts.append(f"P:{self.protein_g}g")
            if self.carbs_g is not None:
                parts.append(f"C:{self.carbs_g}g")
            if self.fat_g is not None:
                parts.append(f"F:{self.fat_g}g")
            macros = "  ".join(parts) or "no macros"
        return f"[{self.restaurant_name}] {name} | {price} | {macros}"
