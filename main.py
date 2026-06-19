# Contoh fix - setelah dapat list invoice
customer_count = {}

for invoice in invoices:  # loop tiap invoice
    # Coba beberapa kemungkinan nama field
    customer_name = (
        invoice.get('customer', {}).get('name') or
        invoice.get('customerName') or
        invoice.get('customer') or
        'Unknown'
    )
    customer_count[customer_name] = customer_count.get(customer_name, 0) + 1

# Sort by terbanyak
sorted_customers = sorted(customer_count.items(), key=lambda x: x[1], reverse=True)
top_customer = sorted_customers[0]
