from dataclasses import dataclass
import re

    
@dataclass
class StockBase:
    code: str
    name: str

    def tushare_code(self) -> str:
        """Convert stock code to Tushare format (e.g., '600519' -> '600519.SH')."""
        if self.code.startswith(('6', '9')):
            if self.code.lower().endswith('.sh'):
                return self.code.upper()
            else:
                return f"{self.code}.SH"
        elif self.code.startswith(('0', '3')):
            if self.code.lower().endswith('.sz'):
                return self.code.upper()
            else:
                return f"{self.code}.SZ"
        elif self.code.startswith(('1', '2')):
            if self.code.lower().endswith('.bj'):
                return self.code.upper()
            else:
                return f"{self.code}.BJ"
        elif self.code.lower().startswith('sh.'):
            return self.code.replace('sh.', '') + '.SH'
        elif self.code.lower().startswith('sz.'):
            return self.code.replace('sz.', '') + '.SZ'
        elif self.code.lower().startswith('bj.'):
            return self.code.replace('bj.', '') + '.BJ'
        else:
            raise ValueError(f"Unrecognized stock code format: {self.code}")

    def akshare_code(self) -> str:
        """Convert stock code to AkShare format (e.g., '600519' -> '600519.SH')."""
        return self.code.lower().replace('sh', '').replace('sz', '').replace('bj', '').replace('.', '')


async def extract_stock_info(query: str) -> list[StockBase]:
    """Extract stock information from the query using llm.
    Args:
        query (str): The input query containing stock information.
    Returns:
        list[StockBase]: Extracted stock information or an empty list if not found."""
    
    